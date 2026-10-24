"""The console router (M18.1): `/console/*`, mounted on `dataplatform.status.api:app`.

Read-only by construction: every route is registered through `_page_route`, which allows GET and
HEAD and nothing else, and `tests/unit/test_console_ops.py` walks this router's routes to prove a
POST, PUT, PATCH or DELETE is a 405 on each — a route added later is covered without anyone
remembering to add it to the test.

Every route declares the same dependency callables as the JSON endpoints (`status.deps`) and
reads through the same `dataplatform.status.queries` functions, so a dependency override on the
app reaches both and the page cannot disagree with `/status/*`. The clock is that injected clock
(B10); nothing here reads the wall clock, opens its own connection, or writes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import cache
from typing import Any, Final

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from dataplatform.logging import get_logger
from dataplatform.scheduler.registry import default_registry
from dataplatform.status.console.data import ROUTES as DATA_ROUTES
from dataplatform.status.console.desk import DESK_PATH, desk_page
from dataplatform.status.console.journal import ROUTES as JOURNAL_ROUTES
from dataplatform.status.console.pages import (
    HTMX_FILE,
    HTMX_PATH,
    PAGES,
    render_job_runs,
    render_jobs_table,
    render_not_built,
    render_not_found,
    render_ops,
)
from dataplatform.status.console.portfolio import ROUTES as PORTFOLIO_ROUTES
from dataplatform.status.deps import ClockDep, ConnDep, HeartbeatDep, JudgeDep, SettingsDep
from dataplatform.status.deviations import DeviationJudge
from dataplatform.status.models import JobLineOut
from dataplatform.status.queries import (
    assess_health,
    is_accepted,
    job_lines,
    read_alerts,
    read_backups,
    read_job_runs,
    sort_worst_first,
)

__all__ = ["READ_METHODS", "router"]

#: The only methods any console route answers (M18 shared rule: read-only until M18.5).
READ_METHODS: Final = ("GET", "HEAD")

router = APIRouter(prefix="/console", include_in_schema=False)

log = get_logger(__name__)


def _page_route(path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
    router.add_api_route(path, endpoint, methods=list(READ_METHODS), **kwargs)


def _lines(
    conn: ConnDep, now: datetime, settings: SettingsDep, judge: DeviationJudge
) -> list[JobLineOut]:
    """Every registered job's line, worst first — the same `read_job_health` as `/status/jobs`,
    and the same accepted-deviation verdicts."""
    return sort_worst_first(
        job_lines(conn, default_registry(), now=now, timezone=settings.tzinfo, judge=judge)
    )


def ops_page(
    conn: ConnDep, clock: ClockDep, settings: SettingsDep, heartbeat: HeartbeatDep, judge: JudgeDep
) -> HTMLResponse:
    """The Operations page: health, every registered job, open alerts, backups."""
    now = clock.now()
    lines = _lines(conn, now, settings, judge)
    by_name = {line.name: line for line in lines}
    page = render_ops(
        health=assess_health(
            heartbeat, now=now, stale_after=settings.scheduler_heartbeat_stale_after_seconds
        ),
        lines=lines,
        alerts=read_alerts(
            conn,
            now=now,
            provider=settings.alert_provider.value,
            failure_alerts=by_name.get("failure_alerts"),
            judge=judge,
        ),
        backups=read_backups(settings, now=now, lines=by_name),
        as_of=now,
    )
    log.info(
        "console.ops_rendered",
        jobs=len(lines),
        unhealthy=sum(not x.healthy for x in lines),
        accepted=sum(not x.healthy and is_accepted(x) for x in lines),
    )
    return HTMLResponse(page)


def jobs_fragment(
    conn: ConnDep, clock: ClockDep, settings: SettingsDep, judge: JudgeDep
) -> HTMLResponse:
    """The jobs table alone — what the Ops page's htmx refresh swaps in."""
    now = clock.now()
    return HTMLResponse(render_jobs_table(_lines(conn, now, settings, judge), as_of=now))


def job_page(
    name: str, conn: ConnDep, clock: ClockDep, settings: SettingsDep, judge: JudgeDep
) -> HTMLResponse:
    """One registered job's newest `job_run` rows; 404 for a name the registry does not hold."""
    now = clock.now()
    registry = default_registry()
    if name not in registry:
        return HTMLResponse(
            render_not_found(f"no registered job named {name!r}", as_of=now), status_code=404
        )
    lines = job_lines(conn, registry, now=now, timezone=settings.tzinfo, judge=judge)
    line = next(item for item in lines if item.name == name)
    return HTMLResponse(render_job_runs(line, read_job_runs(conn, registry.get(name), now=now)))


@cache
def _htmx_bytes() -> bytes:
    return HTMX_PATH.read_bytes()


def htmx_script() -> Response:
    """The vendored htmx build, from the checked-in file (never a CDN)."""
    return Response(
        _htmx_bytes(),
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _not_built(slug: str, label: str) -> Callable[[ClockDep], HTMLResponse]:
    def endpoint(clock: ClockDep) -> HTMLResponse:
        return HTMLResponse(render_not_built(slug, label, as_of=clock.now()))

    endpoint.__name__ = f"{slug}_page"
    return endpoint


_page_route("", ops_page, response_class=HTMLResponse, name="console_home")
_page_route("/ops", ops_page, response_class=HTMLResponse, name="console_ops")
_page_route("/ops/jobs", jobs_fragment, response_class=HTMLResponse, name="console_jobs")
_page_route("/ops/jobs/{name}", job_page, response_class=HTMLResponse, name="console_job")
_page_route(f"/static/{HTMX_FILE}", htmx_script, name="console_htmx")
_page_route(DESK_PATH, desk_page, response_class=HTMLResponse, name="console_desk")
for _slug, _label, _built in PAGES:
    if not _built:
        _page_route(f"/{_slug}", _not_built(_slug, _label), response_class=HTMLResponse, name=_slug)
for _path, _endpoint, _name in DATA_ROUTES:  # M18.2: /console/data
    _page_route(_path, _endpoint, response_class=HTMLResponse, name=_name)
for _path, _endpoint, _name in JOURNAL_ROUTES:  # M18.4
    _page_route(_path, _endpoint, response_class=HTMLResponse, name=_name)
for _path, _endpoint, _name in PORTFOLIO_ROUTES:  # M18.7
    _page_route(_path, _endpoint, response_class=HTMLResponse, name=_name)
