"""Daily capture of the VERIFIED sources nothing ever fetched (ops-daily-capture, 2026-10-06).

Nine Source Register rows had a parser, a frozen fixture and a live 200 — and not one byte in L0,
because no scheduled job called them (`ops/gates/macro-news-ingestion-plan-2026-09-07.md` §1).
Four of them are **perishable**: the endpoint serves only the latest session or the latest state,
so every day nobody captured is history destroyed. This module is the scheduled driver for all
nine, in four jobs (`dataplatform.scheduler.registry`):

* `nse_daily_capture` — FII/DII flows, bulk deals, block deals (perishable) and the current
  session's F&O bhavcopy (dated, but the historical backfill is a separate campaign's).
* `shareholding_poll` — the latest-state shareholding master, weekly.
* `announcements_capture` — NSE and BSE announcements for the previous calendar day, complete.
* `news_capture` — the ratified curated RSS feeds and a bounded GDELT export sample.

Why it has its own drivers instead of calling each module's `ingest_*` function: those were written
for one-shot use and, run daily, each breaks in a way only a second day shows.

* **L0 keys.** `rss.ingest_feed` stores `rbi_press_releases.xml` and `gdelt.ingest_slice` stores
  `lastupdate.txt` — undated names, so the second day of a month collides with the first and
  `L0Store.put` raises. Here every undated payload is named for the instant it was captured.
* **Stale 200s.** `fii_dii.ingest_day` names its file for the session it *asked* for. When the
  endpoint is late and still serves yesterday, yesterday's bytes land under today's name, the day is
  FAILED, and the retry can never succeed — the same key with different bytes raises. Here a
  latest-only payload is filed under its capture instant and *published under the session it
  states*: a 13:00 run files yesterday's flows as yesterday's, which is the copy about to vanish.
* **One L1 partition per date.** `announcements`, `news` and `deals` hold one file per date, and
  each module's writer replaces it whole — so writing BSE after NSE, or the second GDELT slot after
  the first, erased the earlier rows. Here each date's partition is re-derived from every payload
  L0 holds for it, so the write is whole and the order does not matter.
* **Shareholding partitions** are by filing date, and the master shows each company's latest filing
  only, so a later poll writing a filing date's partition from its own rows would drop a company
  that has since filed again. Here a poll merges into what the partition already holds.

Two rules apply to every source, and both are the daily snapshot's (`daily_snapshot.py`):

* **Every outcome reaches `sync_state`**, per `(source, date)`, so `/status/sources` and
  `/status/jobs` see a miss. A host whose lease another driver holds fails its own sources and
  leaves the others running; the job raises at the end if anything is owed, so the run is FAILED
  and the next fire is the retry.
* **Nothing intraday is published as final.** A latest-only payload stamped *today* before
  `CAPTURE_CUTOFF` is kept in L0 and not published, because block deals and flows are still
  accruing — publishing a partial day would make the evening run skip the complete one.

Offline by construction (B8): every `capture_*` function takes a `CaptureContext` whose fetcher
factory, tracker, alerter and identity master are injected; the `run_*_job` entry points build the
real wiring. The clock is injected (B10). Joins stay on ISIN (#2) — deals and BSE announcements
resolve through the D2 master and quarantine what it does not know.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from dataplatform.alerts import Alerter, AlertOutcome, Severity, build_alerter
from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.identity.master import IdentityMaster, IdentityStore
from dataplatform.ingest import announcements as ann
from dataplatform.ingest import gdelt, rss, shareholding
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.corp_actions import build_scrip_index
from dataplatform.ingest.daily_snapshot import LakeRootMismatchError, SnapshotTracker
from dataplatform.ingest.fetcher import Fetcher, FetchHTTPError, leased_fetcher
from dataplatform.ingest.lease import HostBusyError
from dataplatform.ingest.models import IngestError, ParseError
from dataplatform.ingest.news import NewsBatch, write_l1_merged
from dataplatform.ingest.nse import deals, fii_dii, fo_bhavcopy
from dataplatform.ingest.source_register import Source, SourceRegister, Status
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncState, SyncStateStore
from dataplatform.store import fo_aggregates
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store

if TYPE_CHECKING:  # imported lazily by the registry to avoid a scheduler→ingest import cycle
    from dataplatform.scheduler.registry import JobContext

__all__ = [
    "ANNOUNCEMENT_LOOKBACK_DAYS",
    "ANNOUNCEMENT_SOURCES",
    "BSE_MAX_PAGES",
    "CAPTURE_CUTOFF",
    "NEWS_SOURCES",
    "NSE_DAILY_SOURCES",
    "SHAREHOLDING_SOURCES",
    "CaptureContext",
    "CaptureOutcome",
    "CaptureReport",
    "CaptureStatus",
    "DailyCaptureError",
    "FetcherFactory",
    "capture_announcements",
    "capture_deals",
    "capture_fii_dii",
    "capture_fo_bhavcopy",
    "capture_news",
    "capture_shareholding",
    "owed_session",
    "run_announcements_capture",
    "run_announcements_capture_job",
    "run_news_capture",
    "run_news_capture_job",
    "run_nse_daily_capture",
    "run_nse_daily_capture_job",
    "run_shareholding_poll",
    "run_shareholding_poll_job",
]

_LOG = get_logger(__name__)

#: The IST time after which a session's end-of-day files are taken to be final. FII/DII
#: provisional figures and the day's bulk and block deals are out by the early evening; before this
#: a latest-only payload stamped today is provisional and is not published (module docstring).
CAPTURE_CUTOFF: Final = time(19, 30)

#: The register rows each job keeps current — and therefore what the registry's `covers` claims.
#: `tests/unit/test_scheduler_coverage.py` holds the registry to these tuples.
NSE_DAILY_SOURCES: Final[tuple[str, ...]] = (
    fii_dii.SOURCE_ID,
    deals.BULK_SOURCE_ID,
    deals.BLOCK_SOURCE_ID,
    fo_bhavcopy.FO_SOURCE_ID,
)
SHAREHOLDING_SOURCES: Final[tuple[str, ...]] = (shareholding.SOURCE_ID,)
ANNOUNCEMENT_SOURCES: Final[tuple[str, ...]] = (ann.NSE_SOURCE_ID, ann.BSE_SOURCE_ID)
NEWS_SOURCES: Final[tuple[str, ...]] = ("curated_rss", gdelt.SOURCE_ID)

#: How many calendar days back `announcements_capture` re-drives a day that is not PUBLISHED. The
#: announcement feeds are date-parameterised, so a missed night is recoverable — this is the window
#: in which the job recovers it by itself. Seven covers a lease held across a long weekend.
ANNOUNCEMENT_LOOKBACK_DAYS: Final = 7

#: A hard ceiling on BSE announcement pages per day. ~50 records a page; a heavy results day runs
#: to a few thousand records, so 100 pages is headroom, and the cap is what keeps a payload that
#: misreports its row count from turning the job into an unbounded crawl.
BSE_MAX_PAGES: Final = 100


class CaptureStatus(StrEnum):
    """What one source did on one run. `FAILED` is the only one a run is red for."""

    CAPTURED = "CAPTURED"
    """New payload in L0, parsed, and its date PUBLISHED."""

    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    """The owed date was PUBLISHED before this run — no request, or a duplicate capture kept."""

    PROVISIONAL = "PROVISIONAL"
    """A latest-only payload stamped today, before the cutoff: kept in L0, not published."""

    SKIPPED = "SKIPPED"
    """Nothing owed or nothing attributable — e.g. an empty deals file outside the evening run."""

    FAILED = "FAILED"
    """The owed date did not land. Its `sync_state` row is FAILED with the reason."""


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    """One source on one date — the line an operator and a test both read."""

    source: str
    logical_date: date | None
    status: CaptureStatus
    rows: int = 0
    requests: int = 0
    detail: str = ""

    @property
    def failed(self) -> bool:
        """Whether this outcome leaves an owed date un-landed."""
        return self.status is CaptureStatus.FAILED


@dataclass(slots=True)
class CaptureReport:
    """One job run's outcomes, in the order they happened."""

    job: str
    outcomes: list[CaptureOutcome] = field(default_factory=list)

    @property
    def failed(self) -> tuple[CaptureOutcome, ...]:
        """The outcomes that leave something owed."""
        return tuple(outcome for outcome in self.outcomes if outcome.failed)

    @property
    def requests(self) -> int:
        """Requests this run spent, summed over sources."""
        return sum(outcome.requests for outcome in self.outcomes)

    def summary(self) -> str:
        """One line for a log, a gate note or a commit message."""
        parts = [
            f"{o.source}@{o.logical_date.isoformat() if o.logical_date else '-'}={o.status.value}"
            + (f"({o.rows})" if o.rows else "")
            for o in self.outcomes
        ]
        return f"{self.job}: {', '.join(parts) or 'nothing owed'}; {self.requests} request(s)"


class DailyCaptureError(RuntimeError):
    """A run ended with at least one owed date un-landed. Raised after every source was tried."""


#: Opens a fetcher holding the request budget for `hosts`, named for `command`. Production passes
#: `leased_fetcher`; a test passes a recorded fetcher. A busy host raises `HostBusyError` on enter.
FetcherFactory = Callable[[Sequence[str], str], AbstractContextManager[Fetcher]]


@dataclass(frozen=True, slots=True)
class CaptureContext:
    """Everything a capture needs, injected — and the only way it learns any of it (B8, B10)."""

    l0: L0Store
    tracker: SnapshotTracker
    calendar: TradingCalendar
    clock: Clock
    register: SourceRegister
    fetchers: FetcherFactory
    alerter: Alerter
    master: Callable[[], IdentityMaster]
    commit: Callable[[], None] = lambda: None
    data_root: Path | None = None
    scrip_index: Callable[[], Mapping[str, str]] | None = None

    def bse_scrip_index(self) -> Mapping[str, str]:
        """The BSE scrip→ISIN map, from the injected one or built from the D2 master."""
        return build_scrip_index(self.master()) if self.scrip_index is None else self.scrip_index()


# ── dates ─────────────────────────────────────────────────────────────────────────────────────


def owed_session(calendar: TradingCalendar, now: datetime, cutoff: time = CAPTURE_CUTOFF) -> date:
    """The latest session whose end-of-day files should be final at `now`.

    Today when today is a session and `now` is past `cutoff`; otherwise the last expected-data
    date before today. So the 20:00 run owes tonight's session and a 13:00 catch-up owes
    yesterday's — which is exactly the copy a latest-only endpoint is about to overwrite.
    Raises `CalendarCoverageError` (from the calendar) rather than guessing past its coverage.
    """
    today = now.date()
    if calendar.classify(today).expects_data and now.time() >= cutoff:
        return today
    day = today - timedelta(days=1)
    while not calendar.classify(day).expects_data:
        day -= timedelta(days=1)
    return day


# ── sync helpers ──────────────────────────────────────────────────────────────────────────────


def _begin(ctx: CaptureContext, source: str, day: date) -> bool:
    """Open an attempt for `(source, day)`; False when the date is closed to further attempts.

    PUBLISHED is closed (it has no outgoing edge) and so is a non-retryable FAILED. A row left
    mid-flight by a killed run is failed first, because FETCHED → PENDING is not an edge.
    """
    existing = ctx.tracker.get(source, day)
    if existing is not None:
        if existing.state is SyncState.PUBLISHED:
            return False
        if existing.state is SyncState.FAILED and existing.retryable is False:
            _LOG.warning(
                "capture.closed_failure",
                source=source,
                logical_date=day.isoformat(),
                error=existing.last_error,
                state="FAILED",
            )
            return False
        if existing.state in (SyncState.FETCHED, SyncState.VALIDATED, SyncState.NORMALIZED):
            ctx.tracker.mark_failed(
                source, day, f"interrupted run left the row {existing.state.value}"
            )
    ctx.tracker.begin(source, day)
    return True


def _published(ctx: CaptureContext, source: str, day: date) -> bool:
    record = ctx.tracker.get(source, day)
    return record is not None and record.state is SyncState.PUBLISHED


def _fail(
    ctx: CaptureContext,
    source: str,
    day: date,
    error: str,
    *,
    retryable: bool = True,
    requests: int = 0,
) -> CaptureOutcome:
    """Record `error` on `(source, day)` and return the FAILED outcome.

    A row already PUBLISHED is left alone (the failure is reported, the date is not un-landed).
    Every other state is driven to FAILED through a legal edge, so the miss is on the status
    surface and not only in a log line.
    """
    existing = ctx.tracker.get(source, day)
    if existing is None or existing.state is not SyncState.PUBLISHED:
        reopenable = existing is None or (
            existing.state in (SyncState.FAILED, SyncState.GAP) and existing.retryable is not False
        )
        if reopenable:
            ctx.tracker.begin(source, day)
        current = ctx.tracker.get(source, day)
        if current is not None and current.state is not SyncState.FAILED:
            ctx.tracker.mark_failed(source, day, error, retryable=retryable)
        ctx.commit()
    _LOG.error(
        "capture.failed",
        source=source,
        logical_date=day.isoformat(),
        error=error,
        retryable=retryable,
        state="FAILED",
    )
    return CaptureOutcome(source, day, CaptureStatus.FAILED, requests=requests, detail=error)


def _land(
    ctx: CaptureContext,
    source: str,
    day: date,
    ref: L0Ref,
    write: Callable[[], int],
    *,
    requests: int,
) -> CaptureOutcome:
    """Drive `(source, day)` PENDING → PUBLISHED around `write`, which lands the L1 rows.

    The payload has already parsed when this is called, so VALIDATED is honest. A date already
    PUBLISHED returns ALREADY_PUBLISHED with the bytes kept in L0 as the record of what was served.
    """
    if not _begin(ctx, source, day):
        return CaptureOutcome(
            source,
            day,
            CaptureStatus.ALREADY_PUBLISHED,
            requests=requests,
            detail=f"already published; this capture is kept in L0 as {ref.key}",
        )
    try:
        ctx.tracker.mark_fetched(source, day, checksum=ref.sha256, l0_path=ref.key)
        ctx.tracker.mark_validated(source, day)
        rows = write()
        ctx.tracker.mark_normalized(source, day)
        ctx.tracker.mark_published(source, day)
    except Exception as exc:  # containment: recorded on the row, the sweep goes on
        return _fail(ctx, source, day, f"{type(exc).__name__}: {exc}", requests=requests)
    ctx.commit()
    _LOG.info(
        "capture.published",
        source=source,
        logical_date=day.isoformat(),
        l0_key=ref.key,
        rows=rows,
        state="PUBLISHED",
    )
    return CaptureOutcome(source, day, CaptureStatus.CAPTURED, rows=rows, requests=requests)


def _entry(register: SourceRegister, source_id: str) -> Source:
    entry = next((row for row in register.sources if row.id == source_id), None)
    if entry is None:
        raise IngestError(f"no {source_id!r} entry in the Source Register")
    return entry


def _ref_from_key(l0: L0Store, key: str) -> L0Ref:
    """The L0 ref a `sync_state.l0_path` names (`source/YYYY-MM-DD/filename`)."""
    source, logical, filename = key.split("/", 2)
    return l0.ref_for(source, date.fromisoformat(logical), filename)


def _stamp(now: datetime) -> str:
    """The capture-instant suffix an undated payload's L0 filename carries."""
    return f"{now:%Y%m%dT%H%M%S}"


def _hosts(register: SourceRegister, *source_ids: str) -> tuple[str, ...]:
    return tuple(sorted({_entry(register, source_id).host for source_id in source_ids}))


def _with_fetcher(
    ctx: CaptureContext,
    hosts: Sequence[str],
    command: str,
    owed: Sequence[tuple[str, date]],
    body: Callable[[Fetcher], list[CaptureOutcome]],
) -> list[CaptureOutcome]:
    """Run `body` under the request budget for `hosts`; a busy host fails only its own sources.

    A lease is refused, never queued (`lease.py`), so a host another driver holds is reported —
    each owed `(source, date)` FAILED, retryable, naming the holder — and the job moves on to the
    hosts it can have. Waiting or breaking the lease would be the second driver this exists to stop.
    """
    try:
        with ctx.fetchers(hosts, command) as fetcher:
            return body(fetcher)
    except HostBusyError as exc:
        return [_fail(ctx, source, day, f"HostBusyError: {exc}") for source, day in owed]


# ── FII/DII flows ─────────────────────────────────────────────────────────────────────────────


def capture_fii_dii(ctx: CaptureContext, fetcher: Fetcher, *, owed: date) -> CaptureOutcome:
    """Capture the FII/DII endpoint's one session and publish it under the date it states.

    What it does: makes no request when `owed` is already PUBLISHED; otherwise fetches the latest
    session into L0 under the capture instant, parses it, and publishes the session it names —
    which may be earlier than `owed` (a late endpoint) and is then still worth keeping, because it
    is about to be overwritten. `owed` itself is FAILED (retryable) while the endpoint lags.
    What it never does: publish today's figures before `CAPTURE_CUTOFF`, or file one session's
    numbers under another session's date.
    """
    source = fii_dii.SOURCE_ID
    if _published(ctx, source, owed):
        return CaptureOutcome(source, owed, CaptureStatus.ALREADY_PUBLISHED)
    now = ctx.clock.now()
    try:
        ref = fetcher.fetch(
            source,
            fii_dii.flows_url(ctx.register),
            now.date(),
            filename=f"fiidiiTradeReact_captured_{_stamp(now)}.json",
        )
    except Exception as exc:
        return _fail(ctx, source, owed, f"{type(exc).__name__}: {exc}")
    try:
        day = fii_dii.parse_l0(ctx.l0, ref)
    except Exception as exc:
        # Retryable even for a format break: the next capture is a new payload under a new name,
        # so a bad body today does not make the date unreachable the way a dated 404 would.
        return _fail(ctx, source, owed, f"{type(exc).__name__}: {exc}", requests=1)
    served = day.trade_date
    if served == now.date() and now.time() < CAPTURE_CUTOFF:
        provisional = _provisional(source, served, ref, requests=1)
        if served > owed:
            # The endpoint has already moved past the owed session, so that copy is gone.
            return _fail(
                ctx,
                source,
                owed,
                f"the endpoint has moved on to {served.isoformat()} (provisional); "
                f"{owed.isoformat()} was never captured and cannot be re-fetched",
                retryable=False,
                requests=1,
            )
        return provisional

    outcome = _land(
        ctx,
        source,
        served,
        ref,
        lambda: _write_count(fii_dii.write_l1(day, data_root=ctx.data_root), len(day.rows)),
        requests=1,
    )
    if served < owed:
        return _fail(
            ctx,
            source,
            owed,
            f"the endpoint still serves {served.isoformat()}; {owed.isoformat()} is owed "
            f"(captured {served.isoformat()} as {outcome.status.value})",
            requests=1,
        )
    return outcome


def _provisional(source: str, served: date, ref: L0Ref, *, requests: int) -> CaptureOutcome:
    _LOG.info(
        "capture.provisional",
        source=source,
        logical_date=served.isoformat(),
        l0_key=ref.key,
        cutoff=CAPTURE_CUTOFF.isoformat(),
        state="PROVISIONAL",
    )
    return CaptureOutcome(
        source,
        served,
        CaptureStatus.PROVISIONAL,
        requests=requests,
        detail=f"intraday copy before {CAPTURE_CUTOFF:%H:%M}; kept as {ref.key}, not published",
    )


def _write_count(_path: object, rows: int) -> int:
    return rows


# ── bulk and block deals ──────────────────────────────────────────────────────────────────────


def capture_deals(ctx: CaptureContext, fetcher: Fetcher, *, owed: date) -> list[CaptureOutcome]:
    """Capture the rolling bulk and block files and land each session's deals in one partition.

    What it does: fetches whichever of the two files is not yet PUBLISHED for `owed`, under the
    capture instant; dates each from its own `Date` column; resolves symbols to ISINs through the
    D2 master (unknown symbols are quarantined and counted, never guessed); and writes the
    session's L1 partition from both files — the half captured now and, when the other was
    published earlier, the other half re-read from L0 — so neither half ever erases the other.
    What it assumes: a header-only file is a quiet session, but it carries no date. It is
    attributed to `owed` only on the evening run of `owed` itself; any other empty file is
    SKIPPED, because "no deals" filed under the wrong session would be a fabricated fact.
    """
    outcomes: list[CaptureOutcome] = []
    now = ctx.clock.now()
    evening_of_owed = owed == now.date() and now.time() >= CAPTURE_CUTOFF
    landed: dict[date, dict[str, tuple[L0Ref, tuple[deals.DealRow, ...]]]] = {}
    for source in (deals.BULK_SOURCE_ID, deals.BLOCK_SOURCE_ID):
        if _published(ctx, source, owed):
            outcomes.append(CaptureOutcome(source, owed, CaptureStatus.ALREADY_PUBLISHED))
            continue
        stem = deals.l0_filename(source, now.date()).split("_", 1)[0]
        try:
            ref = fetcher.fetch(
                source,
                deals.deals_url(source, ctx.register),
                now.date(),
                filename=f"{stem}_captured_{_stamp(now)}.csv",
            )
        except Exception as exc:
            outcomes.append(_fail(ctx, source, owed, f"{type(exc).__name__}: {exc}"))
            continue
        try:
            rows = deals.parse_l0(ctx.l0, ref, source=source)
        except Exception as exc:
            outcomes.append(_fail(ctx, source, owed, f"{type(exc).__name__}: {exc}", requests=1))
            continue
        if rows:
            served = rows[0].trade_date
        elif evening_of_owed:
            served = owed
        else:
            outcomes.append(
                CaptureOutcome(
                    source,
                    None,
                    CaptureStatus.SKIPPED,
                    requests=1,
                    detail=f"header-only file outside {owed.isoformat()}'s evening run carries "
                    f"no date; kept as {ref.key}, not attributed",
                )
            )
            continue
        if served == now.date() and now.time() < CAPTURE_CUTOFF:
            outcomes.append(_provisional(source, served, ref, requests=1))
            continue
        landed.setdefault(served, {})[source] = (ref, rows)

    for served, captured in sorted(landed.items()):
        outcomes.extend(_land_deals_day(ctx, served, captured))
    for source in (deals.BULK_SOURCE_ID, deals.BLOCK_SOURCE_ID):
        if not _published(ctx, source, owed) and not any(
            o.source == source and o.logical_date == owed for o in outcomes
        ):
            outcomes.append(
                _fail(
                    ctx,
                    source,
                    owed,
                    f"no {source} capture could be attributed to {owed.isoformat()} on this run "
                    "(the file serves another session, or is empty outside the evening run)",
                )
            )
    return outcomes


def _land_deals_day(
    ctx: CaptureContext,
    served: date,
    captured: Mapping[str, tuple[L0Ref, tuple[deals.DealRow, ...]]],
) -> list[CaptureOutcome]:
    """Write `served`'s partition from both files and publish the halves captured now."""
    opened = [source for source in captured if _begin(ctx, source, served)]
    outcomes = [
        CaptureOutcome(
            source,
            served,
            CaptureStatus.ALREADY_PUBLISHED,
            requests=1,
            detail=f"already published; this capture is kept in L0 as {captured[source][0].key}",
        )
        for source in captured
        if source not in opened
    ]
    if not opened:
        return outcomes
    try:
        for source in opened:
            ref = captured[source][0]
            ctx.tracker.mark_fetched(source, served, checksum=ref.sha256, l0_path=ref.key)
            ctx.tracker.mark_validated(source, served)
        master = ctx.master()
        resolved: list[deals.ResolvedDealRow] = []
        quarantined = 0
        for source in (deals.BULK_SOURCE_ID, deals.BLOCK_SOURCE_ID):
            half = _deals_half(ctx, source, served, captured)
            if half is None:
                continue
            ref, rows = half
            resolution = deals.resolve(rows, master, source=source, l0_key=ref.key)
            resolved.extend(resolution.resolved)
            quarantined += len(resolution.unresolved)
        deals.write_l1(
            deals.DealsDay(trade_date=served, rows=tuple(resolved)), data_root=ctx.data_root
        )
        for source in opened:
            ctx.tracker.mark_normalized(source, served)
            ctx.tracker.mark_published(source, served)
    except Exception as exc:
        return outcomes + [
            _fail(ctx, source, served, f"{type(exc).__name__}: {exc}", requests=1)
            for source in opened
        ]
    ctx.commit()
    _LOG.info(
        "capture.deals_published",
        logical_date=served.isoformat(),
        sources=opened,
        resolved=len(resolved),
        quarantined=quarantined,
        state="PUBLISHED",
    )
    return outcomes + [
        CaptureOutcome(
            source,
            served,
            CaptureStatus.CAPTURED,
            rows=len(captured[source][1]),
            requests=1,
            detail=f"{quarantined} unresolved symbol(s) quarantined" if quarantined else "",
        )
        for source in opened
    ]


def _deals_half(
    ctx: CaptureContext,
    source: str,
    served: date,
    captured: Mapping[str, tuple[L0Ref, tuple[deals.DealRow, ...]]],
) -> tuple[L0Ref, tuple[deals.DealRow, ...]] | None:
    """One file's rows for `served`: captured now, or re-read from the payload published earlier."""
    if source in captured:
        return captured[source]
    record = ctx.tracker.get(source, served)
    if record is None or record.state is not SyncState.PUBLISHED or record.l0_path is None:
        return None
    ref = _ref_from_key(ctx.l0, record.l0_path)
    return ref, deals.parse_l0(ctx.l0, ref, source=source)


# ── F&O bhavcopy, current session only ────────────────────────────────────────────────────────


def capture_fo_bhavcopy(ctx: CaptureContext, fetcher: Fetcher, *, owed: date) -> CaptureOutcome:
    """Land `owed`'s F&O bhavcopy: L0, then the L1 `fo_contracts` partition. One session only.

    What it does: nothing when `owed` is already PUBLISHED (by this job or by the historical
    backfill, which owns every earlier session); reuses the payload when L0 already holds it;
    otherwise one request for the dated file.
    What it never does: walk back. A session this job misses stays for the backfill campaign — the
    archive file is dated and permanent, so a miss here is a delay, not a loss. It does not write
    the L2 `fo_aggregates` either: that is derived (`fo_aggregates.rebuild_l2_from_l1`) and is
    rebuilt with the rest of L2, not on a nightly path.
    """
    source = fo_bhavcopy.FO_SOURCE_ID
    if owed < fo_bhavcopy.FO_ERA_START or _published(ctx, source, owed):
        status = (
            CaptureStatus.SKIPPED
            if owed < fo_bhavcopy.FO_ERA_START
            else CaptureStatus.ALREADY_PUBLISHED
        )
        return CaptureOutcome(source, owed, status)
    url = _entry(ctx.register, source).url_template.replace("{YYYYMMDD}", f"{owed:%Y%m%d}")
    filename = url.rsplit("/", 1)[-1]
    requests = 0
    try:
        if ctx.l0.exists(source, owed, filename):
            ref = ctx.l0.ref_for(source, owed, filename)
        else:
            ref = fetcher.fetch(source, url, owed, filename=filename)
            requests = 1
        rows = fo_bhavcopy.parse_l0(ctx.l0, ref)
    except FetchHTTPError as exc:
        # A 404 for tonight's file means "not yet published"; the 23:00 fire and then the
        # backfill campaign retry it, so it is not a dead end.
        return _fail(ctx, source, owed, f"{type(exc).__name__}: {exc}", requests=1)
    except Exception as exc:
        return _fail(ctx, source, owed, f"{type(exc).__name__}: {exc}", requests=requests)
    return _land(
        ctx,
        source,
        owed,
        ref,
        lambda: _write_count(fo_aggregates.write_l1(rows, data_root=ctx.data_root), len(rows)),
        requests=requests,
    )


def run_nse_daily_capture(ctx: CaptureContext) -> CaptureReport:
    """`nse_daily_capture`: flows on www.nseindia.com, deals and F&O on the archive host.

    Each host is leased on its own, so a campaign holding the archive host fails the deals and F&O
    rows for tonight and leaves the flows — the most perishable of the four — to land regardless.
    """
    report = CaptureReport(job="nse_daily_capture")
    owed = owed_session(ctx.calendar, ctx.clock.now())
    command = f"nse daily capture {owed.isoformat()}"

    def flows(fetcher: Fetcher) -> list[CaptureOutcome]:
        return [capture_fii_dii(ctx, fetcher, owed=owed)]

    def archive(fetcher: Fetcher) -> list[CaptureOutcome]:
        return [
            *capture_deals(ctx, fetcher, owed=owed),
            capture_fo_bhavcopy(ctx, fetcher, owed=owed),
        ]

    archive_sources = (deals.BULK_SOURCE_ID, deals.BLOCK_SOURCE_ID, fo_bhavcopy.FO_SOURCE_ID)
    if not _published(ctx, fii_dii.SOURCE_ID, owed):
        report.outcomes += _with_fetcher(
            ctx,
            _hosts(ctx.register, fii_dii.SOURCE_ID),
            command,
            [(fii_dii.SOURCE_ID, owed)],
            flows,
        )
    else:
        report.outcomes.append(
            CaptureOutcome(fii_dii.SOURCE_ID, owed, CaptureStatus.ALREADY_PUBLISHED)
        )
    if not all(_published(ctx, source, owed) for source in archive_sources):
        report.outcomes += _with_fetcher(
            ctx,
            _hosts(ctx.register, *archive_sources),
            command,
            [(source, owed) for source in archive_sources if not _published(ctx, source, owed)],
            archive,
        )
    else:
        report.outcomes += [
            CaptureOutcome(source, owed, CaptureStatus.ALREADY_PUBLISHED)
            for source in archive_sources
        ]
    return report


# ── shareholding ──────────────────────────────────────────────────────────────────────────────


def capture_shareholding(
    ctx: CaptureContext, fetcher: Fetcher, *, poll_date: date
) -> CaptureOutcome:
    """Poll the shareholding master once and merge it into the filing-date partitions.

    What it does: one request (none when the poll date is PUBLISHED, or its payload is already in
    L0), then for every filing date the payload carries, rewrites that partition as the union of
    what it already held and what this poll says — this poll winning for a company/quarter both
    name. The master shows each company's *latest* filing only, so a partition rewritten from one
    poll's rows would silently drop every company that has filed again since.
    What it never does: move a row to a different filing date. Partitioning by filing date is the
    point-in-time contract (`shareholding.read_pit`); a merge that re-dated a row would break it.
    """
    source = shareholding.SOURCE_ID
    if _published(ctx, source, poll_date):
        return CaptureOutcome(source, poll_date, CaptureStatus.ALREADY_PUBLISHED)
    filename = shareholding.l0_filename(poll_date)
    requests = 0
    try:
        if ctx.l0.exists(source, poll_date, filename):
            ref = ctx.l0.ref_for(source, poll_date, filename)
        else:
            ref = fetcher.fetch(
                source, shareholding.snapshot_url(ctx.register), poll_date, filename=filename
            )
            requests = 1
        snapshot = shareholding.parse_l0(ctx.l0, ref)
    except Exception as exc:
        return _fail(ctx, source, poll_date, f"{type(exc).__name__}: {exc}", requests=requests)
    return _land(
        ctx, source, poll_date, ref, lambda: _merge_shareholding(ctx, snapshot), requests=requests
    )


def _merge_shareholding(ctx: CaptureContext, snapshot: shareholding.ShareholdingSnapshot) -> int:
    by_filing: dict[date, dict[tuple[str, date], shareholding.ShareholdingRow]] = {}
    for row in snapshot.rows:
        by_filing.setdefault(row.filing_date, {})[(row.isin, row.period_end)] = row
    for filing_date, fresh in sorted(by_filing.items()):
        try:
            held = shareholding.read_l1(filing_date, data_root=ctx.data_root)
        except FileNotFoundError:
            held = ()
        merged = {(row.isin, row.period_end): row for row in held}
        merged.update(fresh)
        shareholding.write_l1(
            shareholding.ShareholdingSnapshot(
                source=snapshot.source,
                l0_key=snapshot.l0_key,
                rows=tuple(sorted(merged.values(), key=lambda row: (row.isin, row.period_end))),
            ),
            data_root=ctx.data_root,
        )
    return len(snapshot.rows)


def run_shareholding_poll(ctx: CaptureContext) -> CaptureReport:
    """`shareholding_poll`: one poll of the master for today."""
    report = CaptureReport(job="shareholding_poll")
    today = ctx.clock.today()
    source = shareholding.SOURCE_ID
    if _published(ctx, source, today):
        report.outcomes.append(CaptureOutcome(source, today, CaptureStatus.ALREADY_PUBLISHED))
        return report
    report.outcomes += _with_fetcher(
        ctx,
        _hosts(ctx.register, source),
        f"shareholding poll {today.isoformat()}",
        [(source, today)],
        lambda fetcher: [capture_shareholding(ctx, fetcher, poll_date=today)],
    )
    return report


# ── announcements ─────────────────────────────────────────────────────────────────────────────


def _nse_ann_filename(day: date) -> str:
    return f"{ann.NSE_SOURCE_ID}_{day:%Y%m%d}.json"


def _bse_ann_filename(day: date, page: int) -> str:
    return f"{ann.BSE_SOURCE_ID}_{day:%Y%m%d}_p{page:03d}.json"


def _fetch_or_reuse(
    ctx: CaptureContext, fetcher: Fetcher, source: str, url: str, day: date, filename: str
) -> tuple[L0Ref, int]:
    """The payload for `(source, day, filename)`: from L0 when held (0 requests), else fetched."""
    if ctx.l0.exists(source, day, filename):
        return ctx.l0.ref_for(source, day, filename), 0
    return fetcher.fetch(source, url, day, filename=filename), 1


def _nse_announcements(ctx: CaptureContext, fetcher: Fetcher, day: date) -> tuple[list[L0Ref], int]:
    template = _entry(ctx.register, ann.NSE_SOURCE_ID).url_template
    url = template.replace("{DD-MM-YYYY}", f"{day:%d-%m-%Y}")
    ref, spent = _fetch_or_reuse(ctx, fetcher, ann.NSE_SOURCE_ID, url, day, _nse_ann_filename(day))
    ann.parse_nse_l0(ctx.l0, ref)  # validates; the partition re-parses every payload anyway
    return [ref], spent


def _bse_page_count(payload: bytes, *, filename: str) -> tuple[int, int]:
    """`(records on this page, total records)` from a BSE page's `Table` and `Table1.ROWCNT`."""
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(f"body is not valid JSON: {exc}", filename=filename) from exc
    if not isinstance(document, dict) or not isinstance(document.get("Table"), list):
        raise ParseError("no 'Table' array — the documented empty-success `{}`", filename=filename)
    paging = document.get("Table1")
    total: Any = None
    if isinstance(paging, list) and paging and isinstance(paging[0], dict):
        total = paging[0].get("ROWCNT")
    if not isinstance(total, int) or total < 0:
        raise ParseError(f"Table1.ROWCNT is {total!r}, not a row count", filename=filename)
    return len(document["Table"]), total


def _bse_announcements(ctx: CaptureContext, fetcher: Fetcher, day: date) -> tuple[list[L0Ref], int]:
    """Every BSE page for `day`, paged by `Table1.ROWCNT`, each page its own L0 payload.

    A day with no records is legal only when the exchange was shut; on a session an empty first
    page is the register's empty-success gotcha and raises.
    """
    template = _entry(ctx.register, ann.BSE_SOURCE_ID).url_template.replace(
        "{YYYYMMDD}", f"{day:%Y%m%d}"
    )
    spent = 0
    first, cost = _fetch_or_reuse(
        ctx,
        fetcher,
        ann.BSE_SOURCE_ID,
        template.replace("{N}", "1"),
        day,
        _bse_ann_filename(day, 1),
    )
    spent += cost
    per_page, total = _bse_page_count(ctx.l0.get(first), filename=first.filename)
    if total == 0 or per_page == 0:
        if ctx.calendar.classify(day).expects_data:
            raise ParseError(
                f"BSE served no announcements for session {day.isoformat()}; on a trading day "
                "that is the empty-success gotcha, not a quiet day",
                filename=first.filename,
            )
        return [first], spent
    pages = math.ceil(total / per_page)
    if pages > BSE_MAX_PAGES:
        raise IngestError(
            f"BSE reports {total} announcements for {day.isoformat()} ({pages} pages), past the "
            f"{BSE_MAX_PAGES}-page ceiling; refusing an unbounded crawl"
        )
    refs = [first]
    for page in range(2, pages + 1):
        ref, cost = _fetch_or_reuse(
            ctx,
            fetcher,
            ann.BSE_SOURCE_ID,
            template.replace("{N}", str(page)),
            day,
            _bse_ann_filename(day, page),
        )
        spent += cost
        refs.append(ref)
    return refs, spent


def _announcement_rows(
    ctx: CaptureContext, source: str, refs: Sequence[L0Ref], scrip_index: Mapping[str, str]
) -> tuple[list[ann.AnnouncementRow], int]:
    rows: list[ann.AnnouncementRow] = []
    unresolved = 0
    for ref in refs:
        if source == ann.NSE_SOURCE_ID:
            result = ann.parse_nse_l0(ctx.l0, ref)
        elif _bse_page_count(ctx.l0.get(ref), filename=ref.filename)[0] == 0:
            continue  # a shut day's empty page: legal, and nothing to parse
        else:
            result = ann.parse_bse_l0(ctx.l0, ref, scrip_index=scrip_index)
        rows.extend(result.rows)
        unresolved += len(result.unresolved)
    return rows, unresolved


def _held_announcement_refs(ctx: CaptureContext, source: str, day: date) -> list[L0Ref]:
    """This job's payloads for `(source, day)` already in L0 — a per-symbol campaign's are not."""
    prefix = (
        _nse_ann_filename(day)
        if source == ann.NSE_SOURCE_ID
        else f"{ann.BSE_SOURCE_ID}_{day:%Y%m%d}_p"
    )
    return [
        ref
        for ref in ctx.l0.iter_refs(source, start=day, end=day)
        if ref.filename.startswith(prefix.removesuffix(".json")) and ref.filename.endswith(".json")
    ]


def capture_announcements(
    ctx: CaptureContext, fetchers: Mapping[str, Fetcher | HostBusyError], *, day: date
) -> list[CaptureOutcome]:
    """Land one calendar day of NSE and BSE announcements into that day's one L1 partition.

    What it does: for each exchange not yet PUBLISHED for `day`, gets its payloads (from L0 when
    held, else fetched — one NSE request, one BSE request per page); then writes the partition from
    both exchanges' payloads, so landing BSE after NSE never erases NSE. BSE scrips resolve to ISIN
    through the D2 master; one it does not know is quarantined and counted.
    What it assumes: `day` is complete — the job captures yesterday, after midnight.
    """
    fresh: dict[str, tuple[list[L0Ref], int]] = {}
    outcomes: list[CaptureOutcome] = []
    for source in ANNOUNCEMENT_SOURCES:
        if _published(ctx, source, day):
            outcomes.append(CaptureOutcome(source, day, CaptureStatus.ALREADY_PUBLISHED))
            continue
        fetcher = fetchers[source]
        if isinstance(fetcher, HostBusyError):
            outcomes.append(_fail(ctx, source, day, f"HostBusyError: {fetcher}"))
            continue
        try:
            if source == ann.NSE_SOURCE_ID:
                fresh[source] = _nse_announcements(ctx, fetcher, day)
            else:
                fresh[source] = _bse_announcements(ctx, fetcher, day)
        except Exception as exc:
            outcomes.append(_fail(ctx, source, day, f"{type(exc).__name__}: {exc}"))
    if not fresh:
        return outcomes

    opened = [source for source in fresh if _begin(ctx, source, day)]
    try:
        scrip_index = ctx.bse_scrip_index()
        all_rows: list[ann.AnnouncementRow] = []
        counts: dict[str, tuple[int, int]] = {}
        for source in ANNOUNCEMENT_SOURCES:
            if source in fresh:
                refs = fresh[source][0]
            elif _published(ctx, source, day):
                refs = _held_announcement_refs(ctx, source, day)
            else:
                continue
            rows, unresolved = _announcement_rows(ctx, source, refs, scrip_index)
            all_rows.extend(rows)
            counts[source] = (len(rows), unresolved)
        for source in opened:
            first = fresh[source][0][0]
            ctx.tracker.mark_fetched(source, day, checksum=first.sha256, l0_path=first.key)
            ctx.tracker.mark_validated(source, day)
        ann.write_l1(
            ann.AnnouncementBatch(
                logical_date=day,
                source=ann.ANNOUNCEMENTS_DATASET,
                l0_key=None,
                rows=ann.dedupe(all_rows),
            ),
            data_root=ctx.data_root,
        )
        for source in opened:
            ctx.tracker.mark_normalized(source, day)
            ctx.tracker.mark_published(source, day)
    except Exception as exc:
        return outcomes + [
            _fail(ctx, source, day, f"{type(exc).__name__}: {exc}", requests=fresh[source][1])
            for source in opened
        ]
    ctx.commit()
    return outcomes + [
        CaptureOutcome(
            source,
            day,
            CaptureStatus.CAPTURED,
            rows=counts.get(source, (0, 0))[0],
            requests=fresh[source][1],
            detail=f"{counts[source][1]} unresolved" if counts.get(source, (0, 0))[1] else "",
        )
        for source in opened
    ]


def run_announcements_capture(ctx: CaptureContext) -> CaptureReport:
    """`announcements_capture`: yesterday, plus any un-published day in the lookback window.

    Each exchange's host is leased on its own, so a BSE campaign holding `api.bseindia.com` fails
    the BSE rows and leaves NSE to land; the lookback re-drives them the next night.
    """
    report = CaptureReport(job="announcements_capture")
    today = ctx.clock.today()
    days = [today - timedelta(days=back) for back in range(ANNOUNCEMENT_LOOKBACK_DAYS, 0, -1)]
    owed = [
        day
        for day in days
        if not all(_published(ctx, source, day) for source in ANNOUNCEMENT_SOURCES)
    ]
    if not owed:
        report.outcomes += [
            CaptureOutcome(source, days[-1], CaptureStatus.ALREADY_PUBLISHED)
            for source in ANNOUNCEMENT_SOURCES
        ]
        return report
    command = f"announcements capture {owed[0].isoformat()}..{owed[-1].isoformat()}"
    with _announcement_fetchers(ctx, command) as fetchers:
        for day in owed:
            report.outcomes += capture_announcements(ctx, fetchers, day=day)
    return report


@contextmanager
def _announcement_fetchers(
    ctx: CaptureContext, command: str
) -> Iterator[dict[str, Fetcher | HostBusyError]]:
    """One fetcher per announcement host, each leased on its own; a busy one is the error."""
    by_host: dict[str, Fetcher | HostBusyError] = {}
    with ExitStack() as stack:
        for source in ANNOUNCEMENT_SOURCES:
            host = _entry(ctx.register, source).host
            if host in by_host:
                continue
            try:
                by_host[host] = stack.enter_context(ctx.fetchers((host,), command))
            except HostBusyError as exc:
                by_host[host] = exc
        yield {
            source: by_host[_entry(ctx.register, source).host] for source in ANNOUNCEMENT_SOURCES
        }


# ── news: curated RSS + a bounded GDELT sample ────────────────────────────────────────────────


def _ratified_feeds(ctx: CaptureContext) -> tuple[rss.Feed, ...]:
    """The active feeds whose register row is VERIFIED — the only ones this job may fetch.

    `active: true` in `rss_feeds.yaml` is not enough on its own: a feed is fetched under its
    `source` row's crawl policy, and a row that is not VERIFIED has no policy anyone signed off.
    """
    feeds: list[rss.Feed] = []
    for feed in rss.active_feeds():
        entry = next((row for row in ctx.register.sources if row.id == feed.source), None)
        if entry is None or entry.status is not Status.VERIFIED or entry.host != feed.host:
            _LOG.warning("capture.feed_not_ratified", feed=feed.id, source=feed.source)
            continue
        feeds.append(feed)
    return tuple(feeds)


def _rss_unit(feed: rss.Feed, now: datetime) -> str:
    """The sync key of one feed poll: four polls a day, each its own row under the feed's source."""
    return f"{feed.source}/{feed.id}-{now:%H%M}"


def _capture_rss(
    ctx: CaptureContext, fetcher: Fetcher, feed: rss.Feed, now: datetime
) -> CaptureOutcome:
    day = now.date()
    unit = _rss_unit(feed, now)
    if _published(ctx, unit, day):
        return CaptureOutcome(unit, day, CaptureStatus.ALREADY_PUBLISHED)
    try:
        ref = fetcher.fetch(feed.source, feed.url, day, filename=f"{feed.id}_{_stamp(now)}.xml")
        rows = rss.parse_feed_l0(ctx.l0, ref, feed)
    except Exception as exc:
        return _fail(ctx, unit, day, f"{type(exc).__name__}: {exc}", requests=1)
    return _land(ctx, unit, day, ref, lambda: len(rows), requests=1)


def _capture_gdelt(ctx: CaptureContext, fetcher: Fetcher, now: datetime) -> CaptureOutcome:
    """One GDELT slot: the manifest (filed under the capture instant) and the export it names."""
    day = now.date()
    source = gdelt.SOURCE_ID
    manifest_url = _entry(ctx.register, source).url_template
    try:
        manifest = fetcher.fetch(
            source, manifest_url, day, filename=f"lastupdate_{_stamp(now)}.txt"
        )
        entry = gdelt.export_entry(
            gdelt.parse_manifest(ctx.l0.get(manifest), filename=manifest.filename),
            filename=manifest.filename,
        )
        slot = gdelt.slot_from_export_url(entry.url)
    except Exception as exc:
        unit = f"{source}/manifest-{now:%H%M}"
        return _fail(ctx, unit, day, f"{type(exc).__name__}: {exc}", requests=1)
    unit = f"{source}/{slot:%Y%m%d%H%M%S}"
    if _published(ctx, unit, day):
        return CaptureOutcome(unit, day, CaptureStatus.ALREADY_PUBLISHED, requests=1)
    try:
        export = fetcher.fetch(source, entry.url, day)
        if hashlib.md5(ctx.l0.get(export)).hexdigest() != entry.md5:
            raise ParseError(
                f"export MD5 does not match the manifest's {entry.md5}; a corrupt download is not "
                "news",
                filename=export.filename,
            )
        rows = gdelt.parse_export_l0(ctx.l0, export)
    except Exception as exc:
        return _fail(ctx, unit, day, f"{type(exc).__name__}: {exc}", requests=2)
    return _land(ctx, unit, day, export, lambda: len(rows), requests=2)


def _news_batches(ctx: CaptureContext, day: date, feeds: Sequence[rss.Feed]) -> list[NewsBatch]:
    """Every news payload L0 holds for `day`, parsed — the whole partition, re-derived.

    GDELT exports are re-verified against the MD5 of the day's manifests, so an export whose
    download was corrupt never becomes a row even on a re-derivation; an RSS payload that does not
    parse is left out the same way. Nothing here makes a request.
    """
    batches: list[NewsBatch] = []
    for feed in feeds:
        for ref in ctx.l0.iter_refs(feed.source, start=day, end=day):
            if not (ref.filename.startswith(f"{feed.id}_") and ref.filename.endswith(".xml")):
                continue
            try:
                rows = rss.parse_feed_l0(ctx.l0, ref, feed)
            except ParseError:
                continue
            batches.append(NewsBatch(logical_date=day, source=feed.id, l0_key=ref.key, rows=rows))
    refs = list(ctx.l0.iter_refs(gdelt.SOURCE_ID, start=day, end=day))
    digests: dict[str, str] = {}
    for ref in refs:
        if ref.filename.startswith("lastupdate_"):
            try:
                for item in gdelt.parse_manifest(ctx.l0.get(ref), filename=ref.filename):
                    digests[item.url.rsplit("/", 1)[-1]] = item.md5
            except ParseError:
                continue
    for ref in refs:
        if not ref.filename.endswith(gdelt.EXPORT_SUFFIX):
            continue
        if hashlib.md5(ctx.l0.get(ref)).hexdigest() != digests.get(ref.filename):
            continue
        batches.append(
            NewsBatch(
                logical_date=day,
                source=gdelt.GDELT_SOURCE,
                l0_key=ref.key,
                rows=gdelt.parse_export_l0(ctx.l0, ref),
            )
        )
    return batches


def _poll_feeds(
    ctx: CaptureContext, feeds: Sequence[rss.Feed], now: datetime, fetcher: Fetcher
) -> list[CaptureOutcome]:
    return [_capture_rss(ctx, fetcher, feed, now) for feed in feeds]


def capture_news(ctx: CaptureContext) -> list[CaptureOutcome]:
    """One news poll: each ratified RSS feed once, one GDELT slot, then today's partition rebuilt.

    Each host is leased on its own. The partition is re-derived from every payload of the day, so
    four polls a day add up rather than overwrite one another.
    """
    now = ctx.clock.now()
    day = now.date()
    command = f"news capture {day.isoformat()}"
    feeds = _ratified_feeds(ctx)
    outcomes: list[CaptureOutcome] = []
    for host in sorted({feed.host for feed in feeds}):
        on_host = tuple(feed for feed in feeds if feed.host == host)
        outcomes += _with_fetcher(
            ctx,
            (host,),
            command,
            [(_rss_unit(feed, now), day) for feed in on_host],
            partial(_poll_feeds, ctx, on_host, now),
        )
    outcomes += _with_fetcher(
        ctx,
        _hosts(ctx.register, gdelt.SOURCE_ID),
        command,
        [(f"{gdelt.SOURCE_ID}/manifest-{now:%H%M}", day)],
        lambda fetcher: [_capture_gdelt(ctx, fetcher, now)],
    )
    if any(outcome.status is CaptureStatus.CAPTURED for outcome in outcomes):
        write_l1_merged(day, _news_batches(ctx, day, feeds), data_root=ctx.data_root)
    return outcomes


def run_news_capture(ctx: CaptureContext) -> CaptureReport:
    """`news_capture`: one poll."""
    return CaptureReport(job="news_capture", outcomes=capture_news(ctx))


# ── scheduler entry points ────────────────────────────────────────────────────────────────────


def _alert_failures(ctx: CaptureContext, report: CaptureReport) -> int:
    """One alert per failed `(source, date)`, deduplicated per day so a re-run is not new news."""
    sent = 0
    for outcome in report.failed:
        on = outcome.logical_date.isoformat() if outcome.logical_date else "-"
        result = ctx.alerter.send(
            Severity.WARNING,
            f"Daily capture: {outcome.source} FAILED for {on}",
            f"{report.job} could not land {outcome.source} for {on}: {outcome.detail}",
            f"capture:{outcome.source}:{on}",
        )
        sent += int(result is AlertOutcome.SENT)
    return sent


@contextmanager
def _production(context: JobContext) -> Iterator[CaptureContext]:
    """The real wiring: the declared lake, Postgres sync state, leased fetchers, the D2 master."""
    settings: Settings = context.settings
    clock: Clock = context.clock
    register = load_register()
    l0 = L0Store(clock=clock, data_root=settings.data_root)
    expected = settings.snapshot_expect_lake_root
    if expected is not None and l0.root.resolve() != expected.resolve():
        raise LakeRootMismatchError(
            f"L0 resolved to {l0.root.resolve()} but the operator declared {expected.resolve()}; "
            "nothing was fetched. Fix the invocation (DATA_ROOT), not this assertion."
        )
    alerter = build_alerter(settings, clock=clock)
    calendar = trading_calendar()
    with connection(settings) as conn:
        cache: list[IdentityMaster] = []

        def master() -> IdentityMaster:
            if not cache:
                cache.append(IdentityStore(conn, clock=clock).load_master())
            return cache[0]

        def fetchers(hosts: Sequence[str], command: str) -> AbstractContextManager[Fetcher]:
            return leased_fetcher(
                hosts,
                clock=clock,
                command=command,
                settings=settings,
                l0=l0,
                alerter=alerter,
                register=register,
            )

        yield CaptureContext(
            l0=l0,
            tracker=SyncStateStore(conn, clock=clock, calendar=calendar),
            calendar=calendar,
            clock=clock,
            register=register,
            fetchers=fetchers,
            alerter=alerter,
            master=master,
            commit=conn.commit,
            data_root=settings.data_root,
        )
        conn.commit()


def _run_job(context: JobContext, run: Callable[[CaptureContext], CaptureReport]) -> None:
    with _production(context) as ctx:
        report = run(ctx)
        alerts = _alert_failures(ctx, report)
    _LOG.info(
        "capture.job_done",
        job=report.job,
        summary=report.summary(),
        failed=len(report.failed),
        alerts_sent=alerts,
        state="DONE" if not report.failed else "DEGRADED",
    )
    if report.failed:
        raise DailyCaptureError(
            f"{report.summary()} — {len(report.failed)} owed date(s) did not land; each is FAILED "
            "in sync_state and the next fire retries it"
        )


def run_nse_daily_capture_job(context: JobContext) -> None:
    """Scheduler body for `nse_daily_capture`."""
    _run_job(context, run_nse_daily_capture)


def run_shareholding_poll_job(context: JobContext) -> None:
    """Scheduler body for `shareholding_poll`."""
    _run_job(context, run_shareholding_poll)


def run_announcements_capture_job(context: JobContext) -> None:
    """Scheduler body for `announcements_capture`."""
    _run_job(context, run_announcements_capture)


def run_news_capture_job(context: JobContext) -> None:
    """Scheduler body for `news_capture`."""
    _run_job(context, run_news_capture)
