"""D1 (M3.9.b): the benchmark-TRI backfill runner — one request per index, whole history.

`indices.ingest_tri` drives one index from nothing to `PUBLISHED`. This is the driver over a set of
them: it resolves the window, skips an index already published for it, leases the host so no second
driver competes for the request budget, commits after each index, and prints what it landed.

It is a *tiny* campaign and that is the whole point of the source. One POST returns an index's
entire published history — 6,764 rows and ~940 KB for NIFTY 50 back to 1999-06-30 — so the three
indices the reference case needs (§8's NIFTY 50 + NIFTY IT + NIFTY CPSE) cost three requests, not
three thousand. D8 is explicit that re-fetching later is the expensive path, so the default window
starts below any plausible index launch and takes everything the endpoint will give.

Three requests is far under the ~200-request threshold AGENTIC_CONTEXT reserves to the owner
(§4), so this needs no sign-off — but it is still a driver run, not a test. It opens sockets, and
the offline suite never reaches it: `run_tri_backfill` takes its `Fetcher`, `L0Store` and
`SyncTracker` by injection so a test drives it with a `RecordedTransport`, and `main` is the only
place that builds the real, networked wiring (B8). The clock is injected (B10) and reaches only the
L0 receipt and the sync row — never a `knowable_date`, which `indices.tri_knowable_date` derives
from each published row's own session date.

`--from-l0` is the same driver with the fetch removed: it re-derives L1 from the payloads already
in this lake's L0 rather than asking the source again. That is the honest way to promote a run into
a second lake (invariant #1 — L0 is the record, L1 is a derivation of it), and it is also the
cheaper one, because a re-fetch would mint fresh receipts and the endpoint's per-request
`RequestNumber` means the same logical history returns as different bytes. It writes no sync row;
`rebuild_tri_from_l0` says why.

`run_tri_evening` (M13.7) is the same pipeline on a short window, owed every weekday evening: NSE
Indices disseminates session D's TRI on D's evening, so the paper session's regime filter can read
D's published level the night it decides D rather than the next Saturday.

Runbook: `ops/runbooks/benchmark-tri.md`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from dataplatform.clock import IST, Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.calendar import TradingCalendar, trading_calendar
from dataplatform.ingest.fetcher import Fetcher, leased_fetcher
from dataplatform.ingest.indices import (
    TRI_METHOD_PUBLISHED,
    TRI_SOURCE_ID,
    SyncTracker,
    TriSeries,
    ingest_tri,
    parse_l0_tri_filename,
    parse_tri_l0,
    read_tri_series,
    tri_state_source,
    write_tri_l1,
)
from dataplatform.ingest.models import IngestError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.db import connection
from dataplatform.store.l0 import L0Ref, L0Store

if TYPE_CHECKING:  # imported lazily by the registry to avoid a scheduler→ingest import cycle
    from dataplatform.scheduler.registry import JobContext

_LOG = get_logger(__name__)

#: The host every request here goes to — leased for the driver's lifetime.
TRI_HOST: Final = "niftyindices.com"


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """One index to backfill: the name to *send*, and the slug the lake files it under.

    `name` is the exchange's own name in CAPS, which is how `getTotalReturnIndexString` wants it
    inbound; it echoes back title-cased and `parse_tri_native` reconciles the two. `slug` is the
    canonical lake identifier, so a change in the site's title casing cannot move a lake path.
    """

    name: str
    slug: str


#: The three indices AGENTIC_CONTEXT §8's ratified reference case needs. `nifty50` is also
#: `backtest.run`'s `_BENCHMARK_TRI_SLUG` — the broad-market benchmark every run is measured
#: against — so it is the one that must exist for the backtest to stop falling back.
DEFAULT_INDEX_SET: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(name="NIFTY 50", slug="nifty50"),
    IndexSpec(name="NIFTY IT", slug="niftyit"),
    IndexSpec(name="NIFTY CPSE", slug="niftycpse"),
)

#: Indices fetched only when named with ``--index`` (X2, 2026-10-05): the size-tier benchmarks the
#: cap-tier strategies are measured against, so a mid- or small-cap book is not judged against the
#: NIFTY 50 alone. Opt-in, so the default run stays the three-request campaign above. NIFTY 500
#: (M17.7) is the series ``BENCH-N500`` (analyst.fundmanager.controls) buys and holds from S0.
OPT_IN_INDEX_SET: Final[tuple[IndexSpec, ...]] = (
    IndexSpec(name="NIFTY MIDCAP 150", slug="niftymidcap150"),
    IndexSpec(name="NIFTY SMALLCAP 250", slug="niftysmallcap250"),
    IndexSpec(name="NIFTY 500", slug="nifty500"),
)

#: How far behind session D the same-evening window starts, at the latest. The window always
#: reaches back to the stored series' last level as well, so a run after missed evenings closes the
#: gap itself; this floor only keeps a few sessions of overlap with what is already in L1, which is
#: cheap (seven NIFTY 50 rows were 1,000 bytes on 2026-10-06) and lets a restated recent level land.
EVENING_OVERLAP_DAYS: Final = 14

#: How far back the session lookups search the calendar for the newest session. A month is wider
#: than any run of consecutive NSE closures, so a `None` answer means "outside calendar coverage",
#: never "the market was shut for longer than we looked".
SESSION_LOOKBACK_DAYS: Final = 31

#: The default window's lower bound: below every NIFTY index's launch, so the endpoint returns
#: whatever depth it actually has rather than whatever we guessed. D8's own probe recorded
#: 2001-04-02 as NIFTY 50's earliest because it *asked* for 2001-04-01; asked from 1990 the same
#: endpoint answers from 1999-06-30. Depth is a measurement, not a documented number.
EARLIEST_REQUESTED: Final = date(1990, 4, 1)


@dataclass(frozen=True, slots=True)
class IndexOutcome:
    """What happened to one index: published with a measured depth, or skipped as already done.

    `l0_key` names the payload the series came from. A fetch leaves it `None` — the receipt is the
    sync row's — while a `--from-l0` rebuild sets it, because *which stored bytes were re-parsed*
    is the only provenance a rebuild has to report and a run over the wrong payload would otherwise
    look identical to a run over the right one.
    """

    spec: IndexSpec
    points: int
    earliest: date | None
    latest: date | None
    skipped: bool
    l0_key: str | None = None

    @property
    def line(self) -> str:
        if self.skipped:
            return f"{self.spec.slug}: skipped — already published for this window"
        origin = f" from {self.l0_key}" if self.l0_key is not None else ""
        return (
            f"{self.spec.slug}: {self.points} points, "
            f"{self.earliest} .. {self.latest} ({self.spec.name}){origin}"
        )


def already_published(
    spec: IndexSpec, start: date, data_root: Path | None, *, through: date | None = None
) -> bool:
    """Whether L1 already holds a *published* series for this index covering `[start, through]`.

    Resume is read off the artefact rather than off the sync row, for the same reason M10.1's
    constituents sweep does: the artefact is what downstream reads, and a sync row that says
    PUBLISHED while the partition is missing is precisely the disagreement a resume check should
    not trust. `method=TRI_METHOD_PUBLISHED` is explicit — a computed-fallback series on disk is
    not a reason to skip fetching the real one.

    Both ends are checked. Until the 2026-10-05 audit only the start was, so a series published
    once was "already published" for every later window too and no run ever advanced it — NIFTY 50,
    IT and CPSE all stood at 2026-09-07 while the market moved on. `through` is the last session
    the series must reach; `None` checks the start alone (the `--from-l0` and test callers).
    """
    series = read_tri_series(spec.slug, date.max, method=TRI_METHOD_PUBLISHED, data_root=data_root)
    if series is None or series.points[0].as_of > start:
        return False
    return through is None or series.points[-1].as_of >= through


def last_session_before(end: date, calendar: TradingCalendar) -> date | None:
    """The newest session strictly before `end` — the level a fetch dated `end` must already carry.

    Strictly before, because session D's level is disseminated after D's close: a run on D's
    evening may honestly not see D yet, and demanding it would re-fetch the whole history every
    night for one missing point. `None` when no session precedes `end` inside coverage.
    """
    start = max(calendar.coverage_start, end - timedelta(days=SESSION_LOOKBACK_DAYS))
    if end <= start:
        return None
    sessions = calendar.expected_data_dates(start, end - timedelta(days=1))
    return sessions[-1] if sessions else None


def run_tri_backfill(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: SyncTracker,
    indices: Sequence[IndexSpec] = DEFAULT_INDEX_SET,
    start: date = EARLIEST_REQUESTED,
    end: date,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
    commit: Callable[[], None] | None = None,
    calendar: TradingCalendar | None = None,
) -> tuple[IndexOutcome, ...]:
    """Backfill each index's published TRI over `[start, end]`, committing after each.

    Returns one `IndexOutcome` per index in the order given. A failure is *not* swallowed: it is
    recorded on the index's own sync row by `ingest_tri` and then re-raised, because a benchmark
    that half-landed is not a partial success — every excess-return figure downstream would be
    struck against whatever did land. A driver that wanted to park one index and continue would be
    a different decision from a different task.
    """
    through = last_session_before(end, trading_calendar() if calendar is None else calendar)
    outcomes: list[IndexOutcome] = []
    for spec in indices:
        if already_published(spec, start, data_root, through=through):
            _LOG.info(
                "tri_backfill.skipped",
                source=TRI_SOURCE_ID,
                index=spec.slug,
                reason="L1 already holds a published series spanning this window",
                state="PUBLISHED",
            )
            outcomes.append(
                IndexOutcome(spec=spec, points=0, earliest=None, latest=None, skipped=True)
            )
            continue

        series: TriSeries = ingest_tri(
            fetcher=fetcher,
            l0=l0,
            tracker=tracker,
            index_name=spec.name,
            index_slug=spec.slug,
            start=start,
            end=end,
            data_root=data_root,
            register=register,
        )
        if commit is not None:
            commit()
        outcomes.append(
            IndexOutcome(
                spec=spec,
                points=len(series.points),
                earliest=series.points[0].as_of,
                latest=series.points[-1].as_of,
                skipped=False,
            )
        )
    return tuple(outcomes)


def run_tri_refresh(context: JobContext) -> None:
    """The scheduler's `tri_refresh` job body: bring every default index's TRI up to date.

    What it does: under the `niftyindices.com` lease, re-runs `run_tri_backfill` over
    `DEFAULT_INDEX_SET` with the window ending on the job's own date (the injected clock, B10). An
    index whose L1 series already reaches the last session before today is skipped without a
    request, so a re-run on the same day is a no-op; one that is behind costs one POST. A failure is
    recorded on that index's sync row and re-raised, so the run is FAILED and `/status/sources`
    shows the row — never a log line nobody reads.
    What it assumes: the database is migrated and the network reachable.
    What it never does: decide the date for itself, or touch any host but the TRI endpoint's.
    """
    settings = context.settings
    clock = context.clock
    register = load_register()
    calendar = trading_calendar()
    with (
        leased_fetcher(
            [TRI_HOST], clock=clock, command="tri_refresh", settings=settings, register=register
        ) as fetcher,
        connection(settings) as conn,
    ):
        outcomes = run_tri_backfill(
            fetcher=fetcher,
            l0=L0Store(clock=clock, data_root=settings.data_root),
            tracker=SyncStateStore(conn, clock=clock, calendar=calendar),
            end=clock.today(),
            data_root=settings.data_root,
            register=register,
            commit=conn.commit,
            calendar=calendar,
        )
    for outcome in outcomes:
        _LOG.info("tri_refresh.index", source=TRI_SOURCE_ID, line=outcome.line)


def latest_session_through(today: date, calendar: TradingCalendar) -> date | None:
    """The newest session on or before `today` — the level the same-evening refresh owes.

    Unlike `last_session_before`, today counts: the evening job runs after D's close and after NSE
    Indices has disseminated D (measured: 2026-10-05's level absent at 16:08 IST on the 5th,
    2026-10-06's present at 20:47 IST on the 6th). On a weekday holiday this is the previous
    session, which the previous evening already landed, so the run is a no-op. `None` when no
    session falls inside coverage.
    """
    start = max(calendar.coverage_start, today - timedelta(days=SESSION_LOOKBACK_DAYS))
    if today < start:
        return None
    sessions = calendar.expected_data_dates(start, today)
    return sessions[-1] if sessions else None


def evening_window_start(spec: IndexSpec, through: date, data_root: Path | None) -> date:
    """Where the same-evening request for `spec` starts: overlapping L1, never leaving a hole.

    The earlier of the stored published series' last level and `through - EVENING_OVERLAP_DAYS`,
    so the window always touches what L1 already holds. An index with no published series at all
    gets the whole-history window, because a short window would land a fragment with nothing
    behind it and `already_published` would then call that fragment the benchmark.
    """
    series = read_tri_series(spec.slug, date.max, method=TRI_METHOD_PUBLISHED, data_root=data_root)
    if series is None:
        return EARLIEST_REQUESTED
    return min(series.points[-1].as_of, through - timedelta(days=EVENING_OVERLAP_DAYS))


class EveningTracker(SyncTracker, Protocol):
    """The sync-state slice `run_tri_evening` drives: `SyncTracker` and the reads healing needs.

    `SyncStateStore` satisfies it structurally; an offline test drives it with an in-memory double.
    """

    def get(self, source: str, logical_date: date) -> SyncRecord | None: ...

    def rows_in_range(
        self, from_date: date, to_date: date, *, sources: Sequence[str] | None = None
    ) -> tuple[SyncRecord, ...]: ...


def heal_missed_sessions(
    tracker: EveningTracker, spec: IndexSpec, series: TriSeries, *, start: date, through: date
) -> tuple[date, ...]:
    """Close earlier retryable FAILED evening rows whose session tonight's payload carries.

    What it does: an evening that never saw its session (published late, host down, box off) leaves
    `nifty_tri_history/<slug>` FAILED for that date, and the gap scans would report it forever even
    after a later evening's window brought the level in. For every such row dated inside tonight's
    window `[start, through)` whose session is a point of the landed `series`, it walks the row
    through the ordinary §4.4 path — `PENDING → FETCHED → VALIDATED → NORMALIZED → PUBLISHED` — with
    tonight's payload (D's own checksum and L0 key) as the receipt, because those are the bytes that
    actually carry the healed session's level. One `tri_evening.healed` event per row.
    What it assumes: D's row is already PUBLISHED with its receipt (call it after `ingest_tri`).
    What it never does: touch a non-retryable failure (a dead end on purpose), a row whose session
    the payload does not carry, or a date outside the window — a Saturday `tri_refresh` row is dated
    by its run, not a session, and stays that job's business.
    """
    source = tri_state_source(spec.slug)
    receipt = tracker.get(source, through)
    if receipt is None or receipt.state is not SyncState.PUBLISHED or receipt.checksum is None:
        raise IngestError(f"{source} {through}: heal called before the session was published")
    carried = {point.as_of for point in series.points}
    healed: list[date] = []
    for row in tracker.rows_in_range(start, through - timedelta(days=1), sources=[source]):
        session = row.logical_date
        if row.state is not SyncState.FAILED or not row.retryable or session not in carried:
            continue
        tracker.begin(source, session)
        tracker.mark_fetched(source, session, checksum=receipt.checksum, l0_path=receipt.l0_path)
        tracker.mark_validated(source, session)
        tracker.mark_normalized(source, session)
        tracker.mark_published(source, session)
        _LOG.info(
            "tri_evening.healed",
            source=source,
            index=spec.slug,
            session=session.isoformat(),
            landed_with=through.isoformat(),
            l0_key=receipt.l0_path,
            previous_error=row.last_error,
            state="PUBLISHED",
        )
        healed.append(session)
    return tuple(healed)


def run_tri_evening(
    *,
    fetcher: Fetcher,
    l0: L0Store,
    tracker: EveningTracker,
    today: date,
    attempt_at: datetime,
    indices: Sequence[IndexSpec] = DEFAULT_INDEX_SET,
    data_root: Path | None = None,
    register: SourceRegister | None = None,
    commit: Callable[[], None] | None = None,
    calendar: TradingCalendar | None = None,
) -> tuple[IndexOutcome, ...]:
    """Land each index's published TRI through the latest session on or before `today` (M13.7).

    What it does: for each index whose L1 series does not yet reach that session D, one POST over
    a short window ending at D (`evening_window_start`), through `ingest_tri` and so through
    L0 → parse → L1 → `sync_state`, with the sync row dated D. An index already at D is skipped
    without a request, so a second fire the same evening is a no-op once the first landed. After a
    landing it logs `tri_evening.first_landed` (the IST instant, for tuning the first fire) and
    heals earlier missed evenings the payload now covers (`heal_missed_sessions`). D is committed
    before healing starts, so a heal that fails is logged and raised without undoing D.
    What it assumes: D's level is disseminated by the time this runs. When it is not, the payload is
    still kept in L0 (filed under `attempt_at`, so a retry later that evening cannot collide with
    it), the row parks `FAILED` retryable and is committed, and `TriNotYetPublishedError`
    propagates — the run is FAILED and `/status` says so.
    What it never does: write L1 from a payload that does not reach D, stamp a knowable date from
    `attempt_at` (it names the L0 file and nothing else), or continue past a failed index — a
    benchmark that half-landed is not a partial success (`run_tri_backfill` says why).
    """
    through = latest_session_through(today, trading_calendar() if calendar is None else calendar)
    if through is None:
        raise IngestError(f"no trading session on or before {today} inside calendar coverage")
    outcomes: list[IndexOutcome] = []
    for spec in indices:
        # The end alone: `already_published` also demands the series *start* by the requested
        # window's start, and no real series begins by 1990, so it would never skip here.
        stored = read_tri_series(
            spec.slug, date.max, method=TRI_METHOD_PUBLISHED, data_root=data_root
        )
        if stored is not None and stored.points[-1].as_of >= through:
            _LOG.info(
                "tri_evening.skipped",
                source=TRI_SOURCE_ID,
                index=spec.slug,
                session=through.isoformat(),
                reason="L1 already holds the published level for this session",
                state="PUBLISHED",
            )
            outcomes.append(
                IndexOutcome(spec=spec, points=0, earliest=None, latest=None, skipped=True)
            )
            continue

        start = evening_window_start(spec, through, data_root)
        try:
            series = ingest_tri(
                fetcher=fetcher,
                l0=l0,
                tracker=tracker,
                index_name=spec.name,
                index_slug=spec.slug,
                start=start,
                end=through,
                data_root=data_root,
                register=register,
                attempt=attempt_at,
                require_through=through,
            )
        except Exception:
            # `ingest_tri` has recorded the FAILED row; the store never commits and `connection`
            # does not commit on the way out of an exception, so without this the row that tells
            # `/status/sync` "D not yet published" would roll back with the run.
            if commit is not None:
                commit()
            raise
        # D is published and L1 holds it: commit now, so nothing after this — healing included —
        # can roll back the row that says so while the level sits in L1.
        if commit is not None:
            commit()
        landed = tracker.get(tri_state_source(spec.slug), through)
        # One event per (index, session), and only here: a later fire finds L1 at D and skips, so
        # this is the first landing by construction. Read after two weeks to tune the first fire.
        _LOG.info(
            "tri_evening.first_landed",
            source=TRI_SOURCE_ID,
            index=spec.slug,
            session=through.isoformat(),
            landed_at_ist=attempt_at.astimezone(IST).isoformat(),
            attempts=None if landed is None else landed.attempts,
            state="PUBLISHED",
        )
        try:
            heal_missed_sessions(tracker, spec, series, start=start, through=through)
        except Exception as exc:
            # Loud, and without undoing D: D is already committed, and the half-walked heal is
            # left uncommitted for the caller's connection to discard. The missed rows stay FAILED
            # and retryable, so the next landing whose window covers them heals them again.
            _LOG.error(
                "tri_evening.heal_failed",
                source=tri_state_source(spec.slug),
                index=spec.slug,
                session=through.isoformat(),
                error=f"{type(exc).__name__}: {exc}",
                state="PUBLISHED",
            )
            raise
        if commit is not None:
            commit()
        outcomes.append(
            IndexOutcome(
                spec=spec,
                points=len(series.points),
                earliest=series.points[0].as_of,
                latest=series.points[-1].as_of,
                skipped=False,
            )
        )
    return tuple(outcomes)


def run_tri_evening_job(context: JobContext) -> None:
    """The scheduler's `tri_evening` job body: today's session's TRI, the same evening (M13.7).

    What it does: under the `niftyindices.com` lease, `run_tri_evening` over `DEFAULT_INDEX_SET`
    for the job's own date (the injected clock, B10), committing after each index.
    What it assumes: the database is migrated and the network reachable.
    What it never does: touch any host but the TRI endpoint's, or change what the Saturday
    `tri_refresh` does — the two write the same L1 partitions from the same published levels.
    """
    settings = context.settings
    clock = context.clock
    register = load_register()
    calendar = trading_calendar()
    with (
        leased_fetcher(
            [TRI_HOST], clock=clock, command="tri_evening", settings=settings, register=register
        ) as fetcher,
        connection(settings) as conn,
    ):
        outcomes = run_tri_evening(
            fetcher=fetcher,
            l0=L0Store(clock=clock, data_root=settings.data_root),
            tracker=SyncStateStore(conn, clock=clock, calendar=calendar),
            today=clock.today(),
            attempt_at=clock.now(),
            data_root=settings.data_root,
            register=register,
            commit=conn.commit,
            calendar=calendar,
        )
    for outcome in outcomes:
        _LOG.info("tri_evening.index", source=TRI_SOURCE_ID, line=outcome.line)


class NoStoredPayloadError(Exception):
    """A `--from-l0` rebuild was asked for an index whose payload is not in this lake's L0."""


def stored_tri_payloads(l0: L0Store, indices: Sequence[IndexSpec]) -> dict[str, tuple[L0Ref, ...]]:
    """Group this lake's stored TRI payloads by index slug, oldest window first.

    Reads `nifty_tri_history` out of L0 and recovers each payload's index from its filename
    (`parse_l0_tri_filename`), because the endpoint is one URL for every index and the name is the
    only place the index was written down. A payload whose slug is not in `indices` is ignored —
    a lake may hold indices this run was not asked about — but a filename that is not an L0 TRI
    name at all raises, rather than being skipped as if it were not there.

    Each index's payloads come back in **fetch order** (`fetched_at`, the receipt's immutable
    first-seen instant), so a caller replaying them writes in the order the live runs did and the
    latest answer wins every date they overlap on. `L0Store.iter_refs` order (month, then filename)
    stood in for that until M13.7, when it stopped being the same thing: the same-evening refresh
    files short windows (`tri_nifty50_20260922_20261006_at…`) beside the weekly whole-history ones
    (`tri_nifty50_19900401_20261010`), which sort by window start, not by when they were fetched.
    The sort is stable, so payloads fetched at one instant keep the filename order.
    """
    wanted = {spec.slug for spec in indices}
    by_slug: dict[str, list[L0Ref]] = {slug: [] for slug in wanted}
    for ref in l0.iter_refs(TRI_SOURCE_ID):
        slug, _start, _end = parse_l0_tri_filename(ref.filename)
        if slug in wanted:
            by_slug[slug].append(ref)
    return {
        slug: tuple(sorted(refs, key=lambda ref: ref.fetched_at)) for slug, refs in by_slug.items()
    }


def rebuild_tri_from_l0(
    *,
    l0: L0Store,
    indices: Sequence[IndexSpec] = DEFAULT_INDEX_SET,
    data_root: Path | None = None,
) -> tuple[IndexOutcome, ...]:
    """Re-derive each index's published L1 series from the payloads already in this lake's L0.

    What it does: for every stored window of every requested index, re-reads the payload through
    `L0Store.get` (which re-verifies the recorded sha256), re-parses it and rewrites the L1
    partitions. Offline by construction — it takes no `Fetcher`, so it cannot reach the network and
    costs no request against a source whose *re-fetch* is the expensive path (D8).

    What it assumes: L0 holds the payloads. An index with none raises `NoStoredPayloadError` rather
    than returning an empty result, because "the lake has no benchmark" and "the rebuild quietly did
    nothing" must not look the same to a caller.

    What it never does: touch `sync_state`. The sync row records an *ingestion*, and no ingestion is
    happening — L0 is unchanged, and §4.4 closes `PUBLISHED` absolutely (`begin` on a published date
    is an illegal transition, by design, because L0 is immutable). Re-deriving L1 from bytes that
    were already fetched, validated and published is a repair of a derived layer, not a new fetch of
    the source; a row claiming otherwise would be a false receipt. It follows that a lake carried to
    a host whose database does not yet know these payloads gets its L1 back from this path and still
    has no sync history — a true statement about that host, and the runbook says so.
    """
    stored = stored_tri_payloads(l0, indices)
    missing = [spec.slug for spec in indices if not stored[spec.slug]]
    if missing:
        raise NoStoredPayloadError(
            f"no L0 payload for {missing} under {TRI_SOURCE_ID} in {l0.root}. A rebuild re-derives "
            "L1 from stored bytes; it cannot invent them. Copy the payloads in from the lake that "
            "fetched them, or run the fetch (`uv run python -m dataplatform.ingest.tri_backfill`)."
        )

    outcomes: list[IndexOutcome] = []
    for spec in indices:
        series: TriSeries | None = None
        last: L0Ref | None = None
        for ref in stored[spec.slug]:
            series = parse_tri_l0(l0, ref, index_name=spec.name, index_slug=spec.slug)
            write_tri_l1(series, data_root=data_root)
            last = ref
            _LOG.info(
                "tri_backfill.rebuilt_from_l0",
                source=TRI_SOURCE_ID,
                index=spec.slug,
                l0_key=ref.key,
                sha256=ref.sha256,
                points=len(series.points),
                earliest=series.points[0].as_of.isoformat(),
                latest=series.points[-1].as_of.isoformat(),
                state="NORMALIZED",
            )
        assert series is not None and last is not None  # `missing` above ruled the empty case out
        outcomes.append(
            IndexOutcome(
                spec=spec,
                points=len(series.points),
                earliest=series.points[0].as_of,
                latest=series.points[-1].as_of,
                skipped=False,
                l0_key=last.key,
            )
        )
    return tuple(outcomes)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: fetch the configured indices' published TRI and print measured depth.

    Wires the real networked fetcher under a `niftyindices.com` lease, the configured lake and the
    Postgres sync state, and commits after each index so a kill loses at most the index in flight.
    `--end` defaults to today from the system clock, which is a property of *this fetch* — the
    levels carry their own knowable dates, derived from their own sessions.
    """
    parser = argparse.ArgumentParser(prog="tri-backfill", description=__doc__)
    parser.add_argument(
        "--index",
        dest="indices",
        action="append",
        default=None,
        metavar="SLUG",
        help="index slug to backfill, repeatable (default: all of "
        f"{' '.join(s.slug for s in DEFAULT_INDEX_SET)}; opt-in: "
        f"{' '.join(s.slug for s in OPT_IN_INDEX_SET)})",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=EARLIEST_REQUESTED,
        help="earliest date to request (default: 1990-04-01 — below every index's launch)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="latest date to request, and the sync row's logical date (default: today)",
    )
    parser.add_argument(
        "--from-l0",
        dest="from_l0",
        action="store_true",
        help="re-derive L1 from the payloads already in L0 instead of fetching: no network, no "
        "request, no sync write. Use it to bring a lake's L1 back after the derived layer was "
        "lost, or to land a run in a second lake without re-fetching (--start/--end are ignored; "
        "the stored windows are whatever was fetched)",
    )
    args = parser.parse_args(argv)

    known = {spec.slug: spec for spec in (*DEFAULT_INDEX_SET, *OPT_IN_INDEX_SET)}
    if args.indices is None:
        indices = DEFAULT_INDEX_SET
    else:
        unknown = [slug for slug in args.indices if slug not in known]
        if unknown:
            print(
                f"unknown index slug(s) {unknown}; known: {sorted(known)}. Add an IndexSpec "
                "rather than passing an unmapped name — the CAPS name sent to the endpoint is "
                "not derivable from the slug.",
                file=sys.stderr,
            )
            return 2
        indices = tuple(known[slug] for slug in args.indices)

    settings: Settings = get_settings()
    clock: Clock = SystemClock()
    register = load_register()
    end = args.end if args.end is not None else clock.now().date()

    if args.from_l0:
        outcomes = rebuild_tri_from_l0(
            l0=L0Store(clock=clock, data_root=settings.data_root),
            indices=indices,
            data_root=settings.data_root,
        )
        for outcome in outcomes:
            print(outcome.line)
        print(
            f"benchmark_tri: {len(outcomes)} index(es) rebuilt from L0 in {settings.data_root}, "
            "0 requests, sync_state untouched"
        )
        return 0

    with (
        leased_fetcher(
            [TRI_HOST], clock=clock, command="tri-backfill", settings=settings, register=register
        ) as fetcher,
        connection(settings) as conn,
    ):
        l0 = L0Store(clock=clock, data_root=settings.data_root)
        sync = SyncStateStore(conn, clock=clock, calendar=trading_calendar())
        outcomes = run_tri_backfill(
            fetcher=fetcher,
            l0=l0,
            tracker=sync,
            indices=indices,
            start=args.start,
            end=end,
            data_root=settings.data_root,
            register=register,
            commit=conn.commit,
        )

    for outcome in outcomes:
        print(outcome.line)
    published = sum(1 for outcome in outcomes if not outcome.skipped)
    print(
        f"benchmark_tri: {published} of {len(outcomes)} index(es) fetched, "
        f"window {args.start} .. {end}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
