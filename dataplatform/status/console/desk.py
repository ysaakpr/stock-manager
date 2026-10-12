"""The console's Desk page (M18.3): `/console/desk`, the trading side, read-only.

One route. It declares the same dependency callables `/status/paper` and `/status/managers`
declare (`status.deps`) and reads through the same functions (`read_paper_status`,
`read_managers_status`), so an override on the app reaches the endpoint and the page at once and
the two cannot disagree. What the endpoints do not carry — positions, the broker-session
interlock's journaled state, the latest orders, the mandates and the model spend — comes from
`status.queries_desk`, on the same connection and the same injected clock (B10).

Display only: the kill switch and the broker session are shown, never controlled. The route is
registered by `console.routes` through its own read-only `_page_route` (GET and HEAD), directly on
the console router rather than as an included sub-router: this FastAPI includes a router as one
opaque entry, which the M18.1 walk over `router.routes` could not see into, and a route that walk
does not reach is a route whose read-only-ness nobody checks.
"""

from __future__ import annotations

from typing import Final

from fastapi.responses import HTMLResponse

from dataplatform.logging import get_logger
from dataplatform.status.console.desk_render import render_desk
from dataplatform.status.deps import ClockDep, ConnDep, M17JournalDep, RosterDep, SettingsDep
from dataplatform.status.queries import read_managers_status, read_paper_status
from dataplatform.status.queries_desk import (
    read_broker_session,
    read_mandates,
    read_model_cost,
    read_orders,
    read_paper_positions,
)

__all__ = ["DESK_PATH", "desk_page"]

DESK_PATH: Final = "/desk"

log = get_logger(__name__)


def desk_page(
    conn: ConnDep,
    clock: ClockDep,
    settings: SettingsDep,
    entries: M17JournalDep,
    roster: RosterDep,
) -> HTMLResponse:
    """The Desk: kill switch and broker session (display only), paper books, M17, orders, cost."""
    now = clock.now()
    paper = read_paper_status(conn, data_root=settings.data_root, as_of=now)
    managers = read_managers_status(entries, roster=roster, as_of=now)
    broker = read_broker_session(conn)
    page = render_desk(
        paper=paper,
        positions=read_paper_positions(conn),
        managers=managers,
        mandates=read_mandates(roster),
        broker=broker,
        orders=read_orders(conn, as_of=now),
        cost=read_model_cost(conn, as_of=now),
        as_of=now,
    )
    log.info(
        "console.desk_rendered",
        paper_books=len(paper.books),
        paper_healthy=paper.healthy,
        switches_tripped=sum(book.kill_switch.tripped is not False for book in paper.books),
        broker_session=broker.state.value,
        m17_decisions=len(managers.decisions),
    )
    return HTMLResponse(page)
