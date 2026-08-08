"""D5: the status API (§4.4) — and the process the `app` container runs.

Six endpoints, and every field on every one of them comes out of a query:

    GET /health                 liveness + scheduler heartbeat age   (scheduler_heartbeat)
    GET /status/sync?date=      all sources for a date, with states  (sync_state, C.2 calendar)
    GET /status/sources         last success, lag, failure streak    (sync_state)
    GET /status/gaps?from=&to=  every missing day, with its reason   (sync_state, calendar, L1)
    GET /status/quality         open D7 sentinel flags               (quality_flag)
    GET /archives?date=         the published bundle's manifest      (archive_bundle)

An empty database answers all six with empty payloads, and that is the intended answer — the one
thing this module may never do is fill a hole with a plausible number. §4.4 exists because the
agent's daily loop reads `/status/sync` before it trades (invariant #10), so a fabricated green
here would be a real trade on data nobody ever fetched.

`/health` is the endpoint that has to work while everything else is broken, so it is the only one
that tolerates a database it cannot read: it answers 503 with a filled-in body saying what it hit,
because a liveness probe that returns a bare 500 tells an operator nothing. It also distinguishes
the three ways there can be no fresh heartbeat — no scheduler has ever run, one ran and stopped,
and we could not check — because only the middle one is an outage.

Structurally: no route writes, and settings, the clock (B10) and the connection are request-scoped
dependencies rather than module globals, which is what lets the integration suite point this same
app at a scratch database and a frozen instant without any route knowing it happened. Reads of
`sync_state` go through `SyncStateStore` and the heartbeat through `dataplatform.scheduler`, so
there is exactly one implementation of each question this API answers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from starlette.requests import Request

from dataplatform.clock import Clock, SystemClock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.calendar import CalendarError
from dataplatform.logging import get_logger
from dataplatform.quality.gaps import GapReportError, GapScanner, LakeL1Presence
from dataplatform.scheduler import Heartbeat, read_heartbeat
from dataplatform.status.models import (
    ArchivesOut,
    DatabaseHealthOut,
    GapsOut,
    HealthOut,
    QualityOut,
    SchedulerHealthOut,
    SchedulerState,
    ServiceStatus,
    SourcesOut,
    SourceStatusOut,
    SyncStatusOut,
)
from dataplatform.status.queries import read_archives, read_quality
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection, connection

__all__ = [
    "app",
    "clock_source",
    "db_connection",
    "gap_scanner",
    "settings_source",
    "sync_store",
]

app = FastAPI(
    title="trading-platform status API",
    version="0.2.0",
    summary="EXECUTION_PLAN.md §4.4 — sync state, source health, gaps, quality, archives.",
)

log = get_logger(__name__)


# ── injected dependencies ───────────────────────────────────────────────────────────────────


def settings_source() -> Settings:
    """The process settings, as a dependency so a test can point the API at a scratch database."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_source)]


def clock_source(settings: SettingsDep) -> Clock:
    """The clock the API measures heartbeat age against (B10 — never `datetime.now()`)."""
    return SystemClock(settings.tzinfo)


ClockDep = Annotated[Clock, Depends(clock_source)]


def db_connection(settings: SettingsDep) -> Iterator[Connection]:
    """One connection per request, closed on the way out.

    A dependency rather than a module-level pool: the status API is low-traffic by design (the
    daily loop calls it once a day), and one honest connection per request keeps a stale handle
    from outliving a database restart. The integration suite overrides it to reach a scratch DB.

    Never committed — nothing on this surface writes, so a request that ends without a commit has
    ended exactly the way every request here is meant to.
    """
    with connection(settings) as conn:
        yield conn


ConnDep = Annotated[Connection, Depends(db_connection)]


def sync_store(conn: ConnDep, clock: ClockDep) -> SyncStateStore:
    """The §4.4 state machine, wired to this request's connection and clock."""
    return SyncStateStore(conn, clock=clock)


SyncStoreDep = Annotated[SyncStateStore, Depends(sync_store)]


def gap_scanner(conn: ConnDep, settings: SettingsDep) -> GapScanner:
    """D7's gap report (M1.11), wired to this request's connection and the configured lake.

    The lake root comes from settings rather than from the process default so a test — and the
    container, whose `/data` is a bind mount — checks the L1 partitions it actually wrote.
    """
    return GapScanner(conn, l1_presence=LakeL1Presence(settings.data_root))


GapScannerDep = Annotated[GapScanner, Depends(gap_scanner)]


async def _unreachable_database(request: Request, exc: Exception) -> Response:
    """Turn a dead database into an explicit 503 rather than an anonymous 500.

    The distinction matters to whoever is paged: 503 with this message says Postgres is not
    answering, which is an infrastructure problem, where a 500 would suggest the API itself is
    broken and send them reading code. `/health` never reaches here — it handles the same failure
    itself so that it can answer with its own contract instead of a bare detail string.
    """
    log.error("status_api.database_unreachable", path=request.url.path, error=str(exc).strip())
    return JSONResponse(
        status_code=503, content={"detail": f"database unreachable: {str(exc).strip()}"}
    )


app.add_exception_handler(psycopg.OperationalError, _unreachable_database)


# ── /health ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _HeartbeatRead:
    """The outcome of one attempt to read the heartbeat — including the attempt failing.

    Three outcomes, not two: a beat, no beat at all, and no answer. Collapsing the last two into
    `None` is exactly the conflation `/health` exists to avoid, because "no scheduler has ever
    run" and "we could not look" have opposite operational meanings.
    """

    reachable: bool
    beat: Heartbeat | None = None
    error: str | None = None


def heartbeat_source(settings: SettingsDep) -> _HeartbeatRead:
    """The freshest scheduler heartbeat, or why it could not be read.

    Deliberately catches `psycopg.Error` rather than only `OperationalError`: an unmigrated
    database answers the connection and then fails the query, and a `/health` that turned that
    into a 500 would hide the single most likely cause of a fresh deployment not working.
    """
    try:
        return _HeartbeatRead(reachable=True, beat=read_heartbeat(settings))
    except psycopg.OperationalError as error:
        log.warning("health.database_unreachable", error=str(error).strip())
        return _HeartbeatRead(reachable=False, error=str(error).strip())
    except psycopg.Error as error:
        log.warning("health.heartbeat_unreadable", error=str(error).strip())
        return _HeartbeatRead(reachable=True, error=str(error).strip())


@app.get(
    "/health",
    summary="Liveness and scheduler heartbeat age",
    responses={503: {"model": HealthOut, "description": "Heartbeat stale, or the database down"}},
)
def health(
    settings: SettingsDep,
    clock: ClockDep,
    read: Annotated[_HeartbeatRead, Depends(heartbeat_source)],
    response: Response,
) -> HealthOut:
    """Report that this process is serving, and whether a scheduler is still beating.

    What it does: measures the newest heartbeat's age against the injected clock and answers 503
    once that age passes `SCHEDULER_HEARTBEAT_STALE_AFTER_SECONDS`, or when the heartbeat could
    not be read at all. The body carries the same verdict as the status code, so a probe reading
    one and a dashboard reading the other cannot disagree.
    What it assumes: the schema is migrated. An empty `scheduler_heartbeat` means no scheduler has
    started yet — reported as `NEVER_RAN`, and not an outage, because a fresh deployment has to be
    able to come up healthy before its scheduler's first tick.
    What it never does: report on evidence it does not have. A heartbeat it could not read is
    `UNKNOWN`, never `NEVER_RAN`, and never a 200.
    """
    now = clock.now()
    stale_after = settings.scheduler_heartbeat_stale_after_seconds
    database = DatabaseHealthOut(reachable=read.reachable, error=read.error)

    if read.error is not None:
        scheduler = SchedulerHealthOut(
            state=SchedulerState.UNKNOWN, stale_after_seconds=stale_after
        )
    elif read.beat is None:
        scheduler = SchedulerHealthOut(
            state=SchedulerState.NEVER_RAN, stale_after_seconds=stale_after
        )
    else:
        age = read.beat.age(now).total_seconds()
        scheduler = SchedulerHealthOut(
            state=SchedulerState.STALE if age > stale_after else SchedulerState.OK,
            scheduler_id=read.beat.name,
            last_beat_at=read.beat.beat_at,
            age_seconds=age,
            stale_after_seconds=stale_after,
        )

    degraded = read.error is not None or scheduler.state is SchedulerState.STALE
    if degraded:
        response.status_code = 503
        log.warning(
            "status_api.unhealthy",
            database_reachable=database.reachable,
            scheduler_state=scheduler.state.value,
            age_seconds=scheduler.age_seconds,
            stale_after_seconds=stale_after,
        )
    return HealthOut(
        status=ServiceStatus.DEGRADED if degraded else ServiceStatus.OK,
        checked_at=now,
        database=database,
        scheduler=scheduler,
    )


# ── /status ─────────────────────────────────────────────────────────────────────────────────


@app.get("/status/sync", summary="Every source's state for one logical date")
def status_sync(
    date: Annotated[date, Query(description="The trading date to report on (YYYY-MM-DD).")],
    store: SyncStoreDep,
    dataset: Annotated[
        list[str] | None,
        Query(
            description="Source ids the caller's decision depends on; repeat for several. "
            "`green` is evaluated over exactly these, and is null when none are given."
        ),
    ] = None,
) -> SyncStatusOut:
    """Every source's state for one date, and the trading interlock's verdict (§4.4).

    What it does: returns the real `sync_state` rows for `date`, what the C.2 calendar calls that
    date, and — when the caller names the datasets its decision depends on — `is_green`'s answer
    together with the reason behind it, which is the line the agent journals on a
    `SKIPPED_DATA_RED` day.
    What it assumes: nothing about the date. One with no rows returns an empty list, not an error.
    What it never does: guess which datasets are "core". Green over no datasets would be vacuously
    true, so it is `null` instead — invariant #10 fails closed, here as everywhere.
    """
    rows = store.rows_for_date(date)
    kind = store.day_kind(date)
    green = store.is_green(date, dataset) if dataset else None
    return SyncStatusOut.of(
        date,
        rows,
        day_kind=None if kind is None else kind.value,
        expects_data=None if kind is None else kind.expects_data,
        green=green,
    )


@app.get("/status/sources", summary="Per-source last success, lag and failure streak")
def status_sources(store: SyncStoreDep, clock: ClockDep) -> SourcesOut:
    """Per-source last success, lag and failure streak (§4.4).

    Lists the sources the platform has actually recorded a row for. A source that has never been
    fetched has nothing true to say about its lag, and an invented line for it would be exactly
    the fabricated status this API must never serve.
    """
    return SourcesOut(
        as_of=clock.today(),
        sources=[SourceStatusOut.of(status) for status in store.source_statuses()],
    )


@app.get("/status/gaps", summary="Every missing (source, date) pair in a range, with its reason")
def status_gaps(
    scanner: GapScannerDep,
    clock: ClockDep,
    from_date: Annotated[
        date | None, Query(alias="from", description="Inclusive. Defaults to today.")
    ] = None,
    to_date: Annotated[
        date | None, Query(alias="to", description="Inclusive. Defaults to today.")
    ] = None,
    source: Annotated[
        list[str] | None,
        Query(
            description="Sources to report on; repeat for several. Defaults to every source "
            "sync_state has ever held a row for."
        ),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=20000, description="Cap on the enumerated unexplained list.")
    ] = 500,
) -> GapsOut:
    """The unexplained set for a range, and the accounting behind it (§4.4, M1.11).

    What it does: asks D7's gap report to classify every `(source, date)` pair in the range —
    against the C.2 calendar, the D1 source register's eras, `sync_state` and the L1 lake — and
    serves the pairs nothing explains, each with its full sync_state history. `fully_explained`
    is the M1 gate's criterion in one field.
    What it assumes: an unbounded range is not what anyone meant, so each end defaults to today
    rather than to all of history. Both ends are inclusive.
    What it never does: answer for a range the trading calendar does not cover. That is a 400
    naming the missing years, because "no holidays that year" would silently invent ~250 sessions
    and report every one of them as a missing day.
    """
    today = clock.today()
    start = today if from_date is None else from_date
    end = today if to_date is None else to_date
    if start > end:
        raise HTTPException(
            status_code=400, detail=f"from={start.isoformat()} is after to={end.isoformat()}"
        )
    try:
        report = scanner.report(start, end, sources=source)
    except (CalendarError, GapReportError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return GapsOut.of(report, limit=limit)


@app.get("/status/quality", summary="Open D7 sentinel flags")
def status_quality(
    conn: ConnDep,
    clock: ClockDep,
    settings: SettingsDep,
    limit: Annotated[
        int | None, Query(ge=1, le=1000, description="Defaults to STATUS_QUALITY_FLAG_LIMIT.")
    ] = None,
) -> QualityOut:
    """Unresolved quality findings, newest first, with the true open total beside them.

    The list is capped so that one bad ingestion day cannot hand a dashboard a hundred thousand
    rows; `open_total` is counted in the database, so the cap changes how much you read and never
    what is true.
    """
    return read_quality(
        conn,
        as_of=clock.now(),
        limit=settings.status_quality_flag_limit if limit is None else limit,
    )


# ── /archives ───────────────────────────────────────────────────────────────────────────────


@app.get("/archives", summary="Manifest of the daily archive bundle for a date")
def archives(
    conn: ConnDep,
    clock: ClockDep,
    date: Annotated[
        date | None, Query(description="Defaults to today on the injected clock.")
    ] = None,
) -> ArchivesOut:
    """The bundle published for one date (§4.5), or `bundle: null` when none has been.

    What it assumes: the publisher (D6/M1.12) records one row per bundle it publishes, carrying
    the manifest it wrote. This endpoint reports that record; it does not walk the lake, because a
    directory that happens to contain files is not the same claim as a manifest with checksums.
    What it never does: serve the files themselves. The download route is M1.12's, and public
    redistribution of exchange data is a legal question reserved to the human (§10,
    AGENTIC_CONTEXT §3.8) — local and personal use only until that is answered.
    """
    return read_archives(conn, clock.today() if date is None else date)
