"""The console's Portfolio page (M18.7): `/console/portfolio?book=<id>`, one M17 book, read-only.

Every book the desk records is a portfolio of its own (owner, 2026-10-13): a manager's primary
and its mirror, a control, a style book and the bench are each selectable and each get the same
view. The route declares the dependencies `/status/managers` declares (`status.deps`: the M17
journal, the roster, the connection, the clock), so an override on the app reaches both; the dry
stream and the lake's closes are two more dependencies declared here, so a test can supply them.

`ROUTES` is registered by `console.routes` through its read-only `_page_route` (GET and HEAD),
directly on the console router so the M18.1 walk over `router.routes` covers it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any, Final

from fastapi import Depends
from fastapi.responses import HTMLResponse

from dataplatform.logging import get_logger
from dataplatform.status.console.pages import render_not_found
from dataplatform.status.console.portfolio_render import render_portfolio
from dataplatform.status.deps import ClockDep, ConnDep, M17JournalDep, RosterDep, SettingsDep
from dataplatform.status.queries_portfolio import (
    LakeSessionPrices,
    SessionPrices,
    UnknownBookError,
    choose_stream,
    read_m17_dry_journal,
    read_portfolio,
)

__all__ = ["PORTFOLIO_PATH", "ROUTES", "m17_dry_journal_source", "portfolio_prices_source"]

PORTFOLIO_PATH: Final = "/portfolio"

log = get_logger(__name__)


def m17_dry_journal_source(
    conn: ConnDep, settings: SettingsDep, roster: RosterDep, live: M17JournalDep
) -> Sequence[Any]:
    """The dry stream's entries — read only when the live stream is empty (it is shown then)."""
    if live:
        return ()
    return read_m17_dry_journal(conn, data_root=settings.data_root, roster=roster)


M17DryJournalDep = Annotated[Sequence[Any], Depends(m17_dry_journal_source)]


def portfolio_prices_source(settings: SettingsDep) -> SessionPrices:
    """The session closes over the configured lake, and the NSE calendar."""
    return LakeSessionPrices(settings.data_root)


PricesDep = Annotated[SessionPrices, Depends(portfolio_prices_source)]


def portfolio_page(
    conn: ConnDep,
    clock: ClockDep,
    live: M17JournalDep,
    dry: M17DryJournalDep,
    roster: RosterDep,
    prices: PricesDep,
    book: str | None = None,
) -> HTMLResponse:
    """One book's holdings, its next-session BUY/SELL and its fund totals; 404 for an unknown
    book."""
    now = clock.now()
    stream, entries = choose_stream(live, dry)
    wanted = None if book is None or not book.strip() else book.strip()
    try:
        portfolio = read_portfolio(
            conn,
            entries=entries,
            stream=stream,
            roster=roster,
            book_id=wanted,
            prices=prices,
            as_of=now,
        )
    except UnknownBookError as error:
        return HTMLResponse(render_not_found(str(error), as_of=now), status_code=404)
    log.info(
        "console.portfolio_rendered",
        book=portfolio.book.book_id,
        stream=stream.value,
        session=None if portfolio.session is None else portfolio.session.isoformat(),
        rows=len(portfolio.rows),
        reconciled=None if portfolio.totals is None else portfolio.totals.reconciled,
    )
    return HTMLResponse(render_portfolio(portfolio))


#: (path, endpoint, name) for `console.routes` to register.
ROUTES: Final[tuple[tuple[str, Callable[..., Any], str], ...]] = (
    (PORTFOLIO_PATH, portfolio_page, "console_portfolio"),
)
