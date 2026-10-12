"""The Journal page's routes (M18.4): `/console/journal`, `/journal/cases`, `/journal/evidence`.

The decision record, read-only. The decision journal is append-only and permanent (invariant
#12), so every handler here reads — through `dataplatform.status.queries_journal`, over the same
injected connection, clock and settings as the JSON endpoints (`status.deps`) — and nothing else.
`ROUTES` is registered on the console router by `routes.py` through its own `_page_route`, which
is what puts these routes under the M18.1 read-only walk: GET and HEAD only. (Not a sub-router:
an included router is not an `APIRoute` on the console router, and the walk would not see it.)

Every parameter is a query string rather than a path segment, and every one is optional: the
search form submits empty fields as empty strings, and an unparseable value is answered with the
page saying what was wrong (400), not with a JSON validation error.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any, Final

from fastapi.responses import HTMLResponse

from dataplatform.logging import get_logger
from dataplatform.status.console.journal_render import (
    render_cases,
    render_evidence_bundle,
    render_evidence_list,
    render_journal,
    render_journal_error,
)
from dataplatform.status.console.pages import render_not_found
from dataplatform.status.deps import ClockDep, ConnDep, SettingsDep
from dataplatform.status.models_journal import DECISION_KINDS, JournalQuery
from dataplatform.status.queries_journal import (
    parse_evidence_ref,
    read_cases,
    read_evidence_bundle,
    read_evidence_list,
    read_journal,
    read_journal_facets,
)

__all__ = ["ROUTES"]

log = get_logger(__name__)


class _BadSearchError(ValueError):
    """A search parameter the page refuses, with the operator-facing reason."""


def _text(value: str | None) -> str | None:
    """A form field: blank means "not filtered"."""
    if value is None or not value.strip():
        return None
    return value.strip()


def _day(name: str, value: str | None) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _BadSearchError(f"{name} must be a date (YYYY-MM-DD), not {text!r}") from None


def _search(
    date_from: str | None,
    date_to: str | None,
    case: str | None,
    manager: str | None,
    kind: str | None,
    before: str | None,
) -> JournalQuery:
    decision = _text(kind)
    if decision is not None and decision not in DECISION_KINDS:
        raise _BadSearchError(f"kind must be one of {', '.join(DECISION_KINDS)}, not {decision!r}")
    cursor = _text(before)
    if cursor is not None and not cursor.isdigit():
        raise _BadSearchError(f"before must be a journal entry id, not {cursor!r}")
    start, end = _day("date_from", date_from), _day("date_to", date_to)
    if start is not None and end is not None and start > end:
        raise _BadSearchError(f"date_from {start} is after date_to {end}")
    return JournalQuery(
        date_from=start,
        date_to=end,
        case_id=_text(case),
        book=_text(manager),
        decision=decision,
        before=None if cursor is None else int(cursor),
    )


def journal_page(
    conn: ConnDep,
    clock: ClockDep,
    date_from: str | None = None,
    date_to: str | None = None,
    case: str | None = None,
    manager: str | None = None,
    kind: str | None = None,
    before: str | None = None,
) -> HTMLResponse:
    """The decision search: one keyset page of the journal, newest first, no-ops included."""
    now = clock.now()
    try:
        query = _search(date_from, date_to, case, manager, kind, before)
    except _BadSearchError as refused:
        return HTMLResponse(render_journal_error(str(refused), as_of=now), status_code=400)
    page = read_journal(conn, query, now=now)
    log.info(
        "console.journal_rendered",
        entries=len(page.entries),
        before=query.before,
        filtered=any(
            v is not None
            for v in (query.date_from, query.date_to, query.case_id, query.book, query.decision)
        ),
    )
    return HTMLResponse(render_journal(page, read_journal_facets(conn)))


def cases_page(conn: ConnDep, clock: ClockDep) -> HTMLResponse:
    """Every case and its lifecycle stage, each linking to its ratification page."""
    return HTMLResponse(render_cases(read_cases(conn, now=clock.now())))


def evidence_page(
    conn: ConnDep, clock: ClockDep, settings: SettingsDep, ref: str | None = None
) -> HTMLResponse:
    """The evidence store's bundles; with `ref`, that one bundle opened (404 if not stored)."""
    now = clock.now()
    wanted = _text(ref)
    if wanted is None:
        return HTMLResponse(
            render_evidence_list(read_evidence_list(conn, settings.data_root, now=now))
        )
    digest = parse_evidence_ref(wanted)
    if digest is None:
        return HTMLResponse(
            render_not_found(
                f"{wanted!r} is not an evidence reference (sha256:<64 hex>)", as_of=now
            ),
            status_code=404,
        )
    bundle = read_evidence_bundle(conn, settings.data_root, digest, now=now)
    if bundle is None:
        return HTMLResponse(
            render_not_found(f"no evidence bundle is stored under {wanted}", as_of=now),
            status_code=404,
        )
    if not bundle.verified:
        log.warning("console.evidence_checksum_mismatch", ref=bundle.ref)
    return HTMLResponse(render_evidence_bundle(bundle))


#: (path under `/console`, endpoint, route name) — registered by `routes.py`.
ROUTES: Final[tuple[tuple[str, Callable[..., Any], str], ...]] = (
    ("/journal", journal_page, "console_journal"),
    ("/journal/cases", cases_page, "console_journal_cases"),
    ("/journal/evidence", evidence_page, "console_journal_evidence"),
)
