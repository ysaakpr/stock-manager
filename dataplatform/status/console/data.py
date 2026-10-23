"""M18.2: the console's Data page — `/console/data` and the ISIN lookup `/console/data/isin`.

Every panel reads through the function its `/status/*` endpoint calls, with the endpoint's own
dependency callables (`status.deps`), so a dependency override on the app reaches both and a panel
cannot disagree with its JSON:

    sync board   read_sync_status          = GET /status/sync?date=
    sources      read_sources_status       = GET /status/sources
    gaps         GapScanner.report/GapsOut = GET /status/gaps?from=&to=
    quality      read_quality              = GET /status/quality
    quarantine   read_quarantine_status    = GET /status/quarantine
    archives     read_archive_list         (links to GET /archives?date=)

A panel whose read fails renders as *unreadable*, with the masked reason, instead of failing the
whole page: the Data page is where an operator goes when something is broken, and a page that
500s because one table is unreadable hides the five that are not. The failure is also logged.

Read-only: GET and HEAD only — `routes.py` registers `ROUTES` through its own `_page_route`, so
the M18.1 walk over the console router covers them. Dates are query parameters, never path
segments, and are parsed here so an empty date field means "the default" rather than a 422.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Annotated, Any, Final

import psycopg
from fastapi import Query
from fastapi.responses import HTMLResponse

from dataplatform.logging import get_logger
from dataplatform.redaction import mask_and_truncate
from dataplatform.status.console.data_render import (
    DataPanels,
    gaps_range_inputs,
    render_archives,
    render_data,
    render_gaps,
    render_isin_page,
    render_quality,
    render_quarantine,
    render_sources,
    render_sync_board,
    render_unreadable,
)
from dataplatform.status.console.pages import render_not_found
from dataplatform.status.deps import (
    ClockDep,
    ConnDep,
    GapScannerDep,
    JudgeDep,
    SettingsDep,
    SyncStoreDep,
)
from dataplatform.status.models import GapsOut
from dataplatform.status.queries import read_quality, read_quarantine_status
from dataplatform.status.queries_data import (
    is_isin,
    latest_session,
    read_archive_list,
    read_isin_lookup,
    read_sources_status,
    read_sync_status,
    search_symbol,
    sort_sources_worst_first,
)
from dataplatform.store.db import Connection

__all__ = [
    "GAPS_DEFAULT_DAYS",
    "GAPS_LIMIT",
    "QUARANTINE_DEFAULT_DAYS",
    "ROUTES",
    "data_page",
    "isin_page",
]

#: The gaps panel's default range ends at the board's date and covers the week before it.
GAPS_DEFAULT_DAYS: Final = 7
#: `/status/gaps`'s own default enumeration cap.
GAPS_LIMIT: Final = 500
#: `/status/quarantine`'s own default lookback.
QUARANTINE_DEFAULT_DAYS: Final = 90
_QUARANTINE_LIMIT: Final = 500

log = get_logger(__name__)


class _BadDateError(ValueError):
    pass


def _parse_date(name: str, raw: str | None) -> date | None:
    if raw is None or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as error:
        raise _BadDateError(f"{name}={raw!r} is not a date (YYYY-MM-DD)") from error


def _panel(panel: str, title: str, conn: Connection, build: Callable[[], str]) -> str:
    """Render one panel, or — when its read fails — say so in its place."""
    try:
        return build()
    except Exception as error:  # one unreadable panel must not blank the page (module doc)
        reason = mask_and_truncate(f"{type(error).__name__}: {error}", 600)
        log.error("console.data_panel_unreadable", panel=panel, error=reason)
        if isinstance(error, psycopg.Error):
            # A failed statement aborts the transaction; roll back so the next panel can read.
            # Nothing here ever wrote, so there is nothing to lose.
            conn.rollback()
        return render_unreadable(panel, title, reason)


def data_page(
    conn: ConnDep,
    clock: ClockDep,
    settings: SettingsDep,
    store: SyncStoreDep,
    scanner: GapScannerDep,
    judge: JudgeDep,
    date_: Annotated[str | None, Query(alias="date")] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query(alias="to")] = None,
) -> HTMLResponse:
    """The Data page for one logical date (default: the latest trading session) and gap range."""
    now = clock.now()
    today = clock.today()
    try:
        chosen = _parse_date("date", date_)
        gaps_from = _parse_date("from", from_)
        gaps_to = _parse_date("to", to)
    except _BadDateError as error:
        return HTMLResponse(render_not_found(str(error), as_of=now), status_code=400)
    board_date = chosen
    if board_date is None:
        board_date = latest_session(store.calendar, today) or today
    gaps_end = board_date if gaps_to is None else gaps_to
    gaps_start = (
        gaps_end - timedelta(days=GAPS_DEFAULT_DAYS - 1) if gaps_from is None else gaps_from
    )

    def sync() -> str:
        return render_sync_board(
            read_sync_status(store, board_date),
            default_date=chosen is None,
            gaps_range=gaps_range_inputs(gaps_start, gaps_end),
        )

    def sources() -> str:
        payload = read_sources_status(store, today, judge=judge)
        return render_sources(payload, sort_sources_worst_first(payload.sources))

    def gaps() -> str:
        if gaps_start > gaps_end:
            raise ValueError(f"from={gaps_start.isoformat()} is after to={gaps_end.isoformat()}")
        report = scanner.report(gaps_start, gaps_end)
        return render_gaps(GapsOut.of(report, limit=GAPS_LIMIT, judge=judge))

    def quality() -> str:
        return render_quality(
            read_quality(conn, as_of=now, limit=settings.status_quality_flag_limit, judge=judge)
        )

    def quarantine() -> str:
        return render_quarantine(
            read_quarantine_status(
                today - timedelta(days=QUARANTINE_DEFAULT_DAYS),
                today,
                limit=_QUARANTINE_LIMIT,
                data_root=settings.data_root,
            )
        )

    def archives() -> str:
        return render_archives(read_archive_list(conn))

    panels = DataPanels(
        sync=_panel("sync", "Sync board", conn, sync),
        sources=_panel("sources", "Sources", conn, sources),
        gaps=_panel("gaps", "Gaps", conn, gaps),
        quality=_panel("quality", "Quality flags", conn, quality),
        quarantine=_panel("quarantine", "Quarantine", conn, quarantine),
        archives=_panel("archives", "Archives", conn, archives),
    )
    log.info(
        "console.data_rendered",
        date=board_date.isoformat(),
        gaps_from=gaps_start.isoformat(),
        gaps_to=gaps_end.isoformat(),
    )
    return HTMLResponse(render_data(panels, as_of=now))


def isin_page(
    conn: ConnDep,
    clock: ClockDep,
    q: Annotated[str | None, Query(max_length=40)] = None,
) -> HTMLResponse:
    """An ISIN's identity record, or the candidate ISINs for a symbol — never data by symbol."""
    now = clock.now()
    query = "" if q is None else q.strip()
    if not query:
        return HTMLResponse(render_isin_page("", as_of=now))
    upper = query.upper()
    kwargs: dict[str, Any]
    try:
        if is_isin(upper):
            kwargs = {"lookup": read_isin_lookup(conn, upper)}
        else:
            kwargs = {"search": search_symbol(conn, query)}
    except Exception as error:  # same contract as a Data panel: say what failed, do not 500
        reason = mask_and_truncate(f"{type(error).__name__}: {error}", 600)
        log.error("console.isin_lookup_unreadable", error=reason)
        kwargs = {"error": f"unreadable: {reason}"}
    log.info("console.isin_lookup", kind="isin" if is_isin(upper) else "symbol")
    return HTMLResponse(render_isin_page(query, as_of=now, **kwargs))


#: (path under /console, endpoint, route name). `routes.py` registers each through its own
#: `_page_route`, so these get exactly the console's read-only methods. Plain routes rather than an
#: included `APIRouter`: FastAPI 0.141 keeps an included router as one opaque `_IncludedRouter`
#: entry, which the M18.1 walk over `router.routes` could not see into.
ROUTES: Final[tuple[tuple[str, Callable[..., HTMLResponse], str], ...]] = (
    ("/data", data_page, "data"),
    ("/data/isin", isin_page, "console_data_isin"),
)
