"""D5: the status API (§4.4) — and the process the `app` container runs.

Six endpoints, and every field on every one of them comes out of a query:

    GET /health                 liveness + scheduler heartbeat age   (scheduler_heartbeat)
    GET /status/sync?date=      all sources for a date, with states  (sync_state, C.2 calendar)
    GET /status/sources         last success, lag, failure streak    (sync_state)
    GET /status/gaps?from=&to=  every missing day, with its reason   (sync_state, calendar, L1)
    GET /status/quality         open D7 sentinel flags               (quality_flag)
    GET /archives?date=         the published bundle's manifest      (archive_bundle)
    GET /status/managers        M17 books, decisions, scoreboard     (decision_journal, M17.6)

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

from datetime import date, timedelta
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import Request

from dataplatform.ingest.calendar import CalendarError
from dataplatform.logging import get_logger
from dataplatform.quality.gaps import GapReportError
from dataplatform.scheduler.registry import default_registry
from dataplatform.status.console import router as console_router
from dataplatform.status.deps import (
    ClockDep,
    ConnDep,
    GapScannerDep,
    HeartbeatDep,
    M17JournalDep,
    RosterDep,
    SettingsDep,
    SyncStoreDep,
    clock_source,
    db_connection,
    gap_scanner,
    heartbeat_source,
    m17_journal_source,
    m17_roster_source,
    settings_source,
    sync_store,
)
from dataplatform.status.models import (
    ArchivesOut,
    GapsOut,
    HealthOut,
    JobsOut,
    ManagersOut,
    PaperOut,
    QualityOut,
    QuarantineOut,
    ServiceStatus,
    SourcesOut,
    SyncStatusOut,
)
from dataplatform.status.queries import (
    assess_health,
    read_archives,
    read_jobs_status,
    read_managers_status,
    read_paper_status,
    read_quality,
    read_quarantine_status,
)
from dataplatform.status.queries_data import read_sources_status, read_sync_status
from dataplatform.status.sync_state import MalformedSyncSourceError
from dataplatform.store.paths import PathLayoutError

__all__ = [
    "app",
    "clock_source",
    "db_connection",
    "gap_scanner",
    "heartbeat_source",
    "m17_journal_source",
    "m17_roster_source",
    "settings_source",
    "sync_store",
]

app = FastAPI(
    title="trading-platform status API",
    version="0.2.0",
    summary="EXECUTION_PLAN.md §4.4 — sync state, source health, gaps, quality, archives.",
)

log = get_logger(__name__)


# ── injected dependencies (shared with the console: `dataplatform.status.deps`) ─────────────


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


@app.get(
    "/health",
    summary="Liveness and scheduler heartbeat age",
    responses={503: {"model": HealthOut, "description": "Heartbeat stale, or the database down"}},
)
def health(
    settings: SettingsDep, clock: ClockDep, read: HeartbeatDep, response: Response
) -> HealthOut:
    """Report that this process is serving, and whether a scheduler is still beating.

    What it does: measures the newest heartbeat's age against the injected clock and answers 503
    once that age passes `SCHEDULER_HEARTBEAT_STALE_AFTER_SECONDS`, or when the heartbeat could
    not be read at all. The body carries the same verdict as the status code, so a probe reading
    one and a dashboard reading the other cannot disagree — and the console's health banner is
    built by the same `assess_health`, so neither can it.
    What it assumes: the schema is migrated. An empty `scheduler_heartbeat` means no scheduler has
    started yet — reported as `NEVER_RAN`, and not an outage, because a fresh deployment has to be
    able to come up healthy before its scheduler's first tick.
    What it never does: report on evidence it does not have. A heartbeat it could not read is
    `UNKNOWN`, never `NEVER_RAN`, and never a 200.
    """
    result = assess_health(
        read, now=clock.now(), stale_after=settings.scheduler_heartbeat_stale_after_seconds
    )
    if result.status is ServiceStatus.DEGRADED:
        response.status_code = 503
        log.warning(
            "status_api.unhealthy",
            database_reachable=result.database.reachable,
            scheduler_state=result.scheduler.state.value,
            age_seconds=result.scheduler.age_seconds,
            stale_after_seconds=result.scheduler.stale_after_seconds,
        )
    return result


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
    return read_sync_status(store, date, dataset)


@app.get("/status/sources", summary="Per-source last success, lag and failure streak")
def status_sources(store: SyncStoreDep, clock: ClockDep) -> SourcesOut:
    """Per-source last success, lag and failure streak (§4.4).

    Lists the sources the platform has actually recorded a row for. A source that has never been
    fetched has nothing true to say about its lag, and an invented line for it would be exactly
    the fabricated status this API must never serve. A source a registered job keeps current is
    `overdue`, and not `healthy`, once it is more sessions behind than that job's budget.

    A source the Source Register DECLINED on policy grounds (D12/D19) is listed under `declined`
    whether or not it has rows, and its line, if any, carries the record and is never red.
    """
    return read_sources_status(store, clock.today())


@app.get("/status/jobs", summary="Every registered job: on schedule, overdue, failing or never run")
def status_jobs(
    conn: ConnDep, store: SyncStoreDep, clock: ClockDep, settings: SettingsDep
) -> JobsOut:
    """Each registered job against its own cron, and the live sources no job keeps current.

    What it does: reads `job_run` for every job in the production registry and reports NEVER_RAN,
    FAILING, OVERDUE, RUNNING or OK (`scheduler.health.assess`), plus the registry's `UNSCHEDULED`
    ledger. A job registered in code that no process ever fires is NEVER_RAN here — the state
    `eod_pipeline` sat in, unseen, from M1.10 to the 2026-10-05 audit.
    What it never does: report only the jobs that happen to have rows; the registry is the list.
    """
    return read_jobs_status(
        conn, default_registry(), now=clock.now(), timezone=settings.tzinfo, declined=store.declined
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
    and report every one of them as a missing day. Nor does it answer a malformed `source` with a
    500: a name that is not a lake identifier is the caller's error and says so (audit finding N1,
    where a poisoned `sync_state` reached `LakeL1Presence` and the stack trace was the whole
    diagnosis an operator got).
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
    except (
        CalendarError,
        GapReportError,
        MalformedSyncSourceError,
        PathLayoutError,
    ) as exc:
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


@app.get("/status/paper", summary="Each paper book: latest session, kill switch, what blocks it")
def status_paper(conn: ConnDep, clock: ClockDep, settings: SettingsDep) -> PaperOut:
    """The paper session's own red (M15.3): a tripped kill switch, a reconciliation break, an
    unresolved escalation.

    A session the switch or a break refused is journaled and recorded red in `paper_session`, but
    nothing about it reaches `/status/jobs` — the job ran fine. This is where it shows: per book,
    the latest session's outcome and reconciliation, the switch's state file (under
    `Settings.data_root`), and every escalation or break with no resolution row.
    What it never does: report an unreadable switch file as armed.
    """
    return read_paper_status(conn, data_root=settings.data_root, as_of=clock.now())


@app.get(
    "/status/managers",
    summary="M17 fund managers: books, the latest session's decisions, the §6 scoreboard",
)
def status_managers(
    entries: M17JournalDep,
    roster: RosterDep,
    clock: ClockDep,
) -> ManagersOut:
    """Every M17 book's latest mark, the latest session's decisions and the scoreboard (M17.6).

    The scoreboard is rebuilt from the journal on each request, exactly as the daily job builds
    it, and reported as "k of 8 books passed" with each manager's both/one/neither, the Amendment
    2 (e) graduation floor and the secondary style books (M17.14). What it never carries: a
    rationale, a prompt, evidence text or a secret — the decisions are their structured fields only.
    """
    return read_managers_status(entries, roster=roster, as_of=clock.now())


# ── /archives ───────────────────────────────────────────────────────────────────────────────


@app.get("/status/quarantine", summary="Rows L1 refused, by session and reason")
def status_quarantine(
    clock: ClockDep,
    settings: SettingsDep,
    from_date: Annotated[
        date | None, Query(alias="from", description="Inclusive. Defaults to 90 days back.")
    ] = None,
    to_date: Annotated[
        date | None, Query(alias="to", description="Inclusive. Defaults to today.")
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=20000, description="Cap on the per-session enumeration.")
    ] = 500,
) -> QuarantineOut:
    """What `prices_raw_quarantine` holds over a range, and which sessions are a step change.

    What it does: counts the dataset by session, exchange and reason, and runs the trailing-median
    rule over each series. `steps` is the actionable field — the *level* of unresolved delivery
    symbols is a known, tracked consequence of the identity master being one snapshot, and a rule
    that fired on it would be muted within a week.
    What it assumes: nothing about the lake existing. A range with no partitions answers zero rows
    over zero partitions and says so, because "we found nothing" and "we looked at nothing" must
    not render identically.
    What it never does: read from Postgres. The quarantine is an L1 dataset; this endpoint is the
    only one here that answers from the lake, which is why it takes no connection.

    Written because the dataset had a writer and no reader: 1.8 M rows across 2,467 partitions,
    one grep away from invisible (2026-09-06 audit, finding N3).
    """
    today = clock.today()
    end = today if to_date is None else to_date
    start = (end - timedelta(days=90)) if from_date is None else from_date
    if start > end:
        raise HTTPException(
            status_code=400, detail=f"from={start.isoformat()} is after to={end.isoformat()}"
        )
    return read_quarantine_status(start, end, limit=limit, data_root=settings.data_root)


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
    What it never does: serve the files themselves. The download route is `/archives/download`, and
    public redistribution of exchange data is a legal question reserved to the human (§10,
    AGENTIC_CONTEXT §3.8) — local and personal use only until that is answered.
    """
    return read_archives(conn, clock.today() if date is None else date)


@app.get(
    "/archives/download",
    summary="Download one file from a published bundle (local/personal use only)",
    responses={404: {"description": "No bundle for the date, or no such file in its manifest"}},
)
def archives_download(
    conn: ConnDep,
    settings: SettingsDep,
    name: Annotated[str, Query(description="A file name from the date's manifest.")],
    date: Annotated[date, Query(description="The bundle's trading date (YYYY-MM-DD).")],
) -> FileResponse:
    """Stream one file of a published bundle, checked against the manifest before it is served.

    What it does: reads the `archive_bundle` row for `date`, confirms `name` is a file the manifest
    lists (which is also what forecloses path traversal — only a manifested name is served, never an
    arbitrary path), resolves it under `<data_root>/<bundle_path>/<name>` and returns it. A caller
    verifies the bytes against the manifest's sha256; this route does not re-hash on the way out.
    What it never does: redistribute publicly. Public redistribution of exchange data is a legal
    question reserved to the human (§10, AGENTIC_CONTEXT §3.8); this endpoint is the operator's own
    host serving the operator's own lake — local and personal download only until that is answered.
    """
    result = read_archives(conn, date)
    if result.bundle is None:
        raise HTTPException(
            status_code=404, detail=f"no archive bundle published for {date.isoformat()}"
        )
    entry = next((file for file in result.bundle.files if file.name == name), None)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"bundle for {date.isoformat()} has no file named {name!r}",
        )
    path = settings.data_root / result.bundle.bundle_path / entry.path
    if not path.is_file():
        log.error(
            "archives.download_missing_file",
            date=date.isoformat(),
            name=name,
            path=str(path),
        )
        raise HTTPException(
            status_code=404,
            detail=f"file {name!r} is in the manifest for {date.isoformat()} but not on disk",
        )
    return FileResponse(path, filename=entry.name, media_type="application/octet-stream")


# ── /console (M18) ──────────────────────────────────────────────────────────────────────────
# The operator console: server-rendered pages over the same queries as the JSON above.
app.include_router(console_router)
