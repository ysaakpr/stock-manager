"""The console's pages as pure functions of the wire models: models in, HTML out.

No route logic, no I/O and no clock: each function takes what `dataplatform.status.queries`
returned and renders it, so a page is tested with constructed models and cannot compute a number
the JSON surface does not also serve. Every free-text field goes through `render.free_text` and
every instant through `render.ist`.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Final

from dataplatform.status.console.render import (
    MISSING,
    age,
    esc,
    fill,
    free_text,
    human_bytes,
    ist,
    seconds,
)
from dataplatform.status.models import (
    AlertsOut,
    BackupsOut,
    BackupStreamOut,
    HealthOut,
    JobLineOut,
    JobRunsOut,
    SchedulerState,
    ServiceStatus,
)

__all__ = [
    "HTMX_FILE",
    "HTMX_PATH",
    "JOBS_REFRESH_SECONDS",
    "PAGES",
    "htmx_integrity",
    "render_job_runs",
    "render_jobs_table",
    "render_not_built",
    "render_not_found",
    "render_ops",
]

#: The vendored htmx build (1.9.12, the version `analyst/cases` pins). Its sha256 is asserted in
#: `tests/unit/test_console_ops.py`; replacing the file without updating that pin fails the suite.
HTMX_FILE: Final = "htmx-1.9.12.min.js"
HTMX_PATH: Final = Path(__file__).parent / "static" / HTMX_FILE

#: How often the Ops page re-reads the jobs table.
JOBS_REFRESH_SECONDS: Final = 30

#: The console's pages in navigation order: (slug, label, built). Later M18 tasks flip `built`.
PAGES: Final[tuple[tuple[str, str, bool], ...]] = (
    ("ops", "Ops", True),
    ("data", "Data", True),
    ("desk", "Desk", True),
    ("journal", "Journal", True),
    ("portfolio", "Portfolio", True),
)

_CURRENT: Final = ' class="current"'

#: Free text in a table cell is cut shorter than in the drill-down, which shows it whole.
_CELL_TEXT: Final = 240


@cache
def htmx_integrity() -> str:
    """The vendored file's SRI digest, so the browser refuses a file that is not the pinned one."""
    digest = hashlib.sha384(HTMX_PATH.read_bytes()).digest()
    return "sha384-" + base64.b64encode(digest).decode("ascii")


def _page(title: str, current: str, body: str, *, as_of: datetime) -> str:
    nav = " ".join(
        f'<a href="/console/{slug}"{_CURRENT if slug == current else ""}>{esc(label)}</a>'
        for slug, label, _ in PAGES
    )
    return fill(
        "base.html",
        {
            "title": esc(title),
            "nav": nav,
            "as_of": ist(as_of),
            "htmx_file": HTMX_FILE,
            "htmx_integrity": htmx_integrity(),
            "body": body,
        },
    )


def _state(value: object) -> str:
    text = esc(value)
    return f'<span class="state state-{text}">{text}</span>'


# ── health banner ──────────────────────────────────────────────────────────────────────────


def _health_banner(health: HealthOut) -> str:
    scheduler = health.scheduler
    ok = health.status is ServiceStatus.OK
    css = "ok" if ok and scheduler.state is SchedulerState.OK else ("warn" if ok else "bad")
    if health.database.reachable:
        database = "database reachable"
    else:
        database = "database <strong>unreachable</strong>"
    if health.database.error is not None:
        database += f': <span class="mono">{free_text(health.database.error, limit=400)}</span>'
    beat = (
        f"scheduler {_state(scheduler.state.value)}"
        f" · newest beat {ist(scheduler.last_beat_at)}"
        f" ({age(scheduler.age_seconds)}, stale after {scheduler.stale_after_seconds} s)"
    )
    if scheduler.scheduler_id is not None:
        beat += f' · <span class="mono">{esc(scheduler.scheduler_id)}</span>'
    return (
        f'<div class="banner {css}" id="health" data-status="{esc(health.status.value)}">'
        f"<strong>{_state(health.status.value)}</strong> · {database} · {beat}"
        f'<div class="muted">same verdict as <span class="mono">/health</span>, '
        f"checked {ist(health.checked_at)}</div></div>"
    )


# ── jobs ───────────────────────────────────────────────────────────────────────────────────


def _job_row(line: JobLineOut) -> str:
    name = esc(line.name)
    last_run = MISSING
    if line.last_started_at is not None:
        last_run = f"{ist(line.last_started_at)} {_state(line.last_state)}"
    return (
        f'<tr data-job="{name}" data-state="{esc(line.state.value)}">'
        f'<td><a href="/console/ops/jobs/{name}">{name}</a></td>'
        f"<td>{_state(line.state.value)}</td>"
        f'<td class="mono">{esc(line.cron)}</td>'
        f"<td>{last_run}</td>"
        f'<td class="num">{seconds(line.last_duration_seconds)}</td>'
        f"<td>{ist(line.last_success_at)}</td>"
        f"<td>{ist(line.due_since)}</td>"
        f"<td>{ist(line.next_fire_at)}</td>"
        f"<td><pre>{free_text(line.last_error, limit=_CELL_TEXT)}</pre></td>"
        "</tr>"
    )


def render_jobs_table(lines: Sequence[JobLineOut], *, as_of: datetime) -> str:
    """The jobs table, worst first as given — also the fragment htmx swaps in every 30 s."""
    unhealthy = sum(1 for line in lines if not line.healthy)
    rows = "".join(_job_row(line) for line in lines)
    return (
        f'<div id="jobs" hx-get="/console/ops/jobs" hx-trigger="every {JOBS_REFRESH_SECONDS}s" '
        'hx-swap="outerHTML">'
        f'<h2>Jobs <span class="muted">{len(lines)} registered, {unhealthy} not healthy · '
        f"read {ist(as_of)}, refreshes every {JOBS_REFRESH_SECONDS} s</span></h2>"
        "<table><thead><tr><th>Job</th><th>State</th><th>Cron</th><th>Last run</th>"
        "<th>Duration</th><th>Last success</th><th>Due since</th><th>Next fire</th>"
        "<th>Last error</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


# ── alerts ─────────────────────────────────────────────────────────────────────────────────


def _alerts_panel(alerts: AlertsOut) -> str:
    provider = f'channel <span class="mono">{esc(alerts.provider)}</span>'
    if alerts.provider == "log":
        provider += " — onsets are written to the log only; nothing pages"
    evaluator = alerts.failure_alerts
    if evaluator is None:
        tick = 'the <span class="mono">failure_alerts</span> job is not registered'
    else:
        tick = (
            f'<span class="mono">failure_alerts</span> {_state(evaluator.state.value)}, '
            f"last tick {ist(evaluator.last_started_at)}"
        )
    rows = "".join(
        f'<tr data-alert="{esc(item.dedup_key)}"><td>{_state(item.severity)}</td>'
        f"<td>{esc(item.trigger)}</td><td>{free_text(item.title, limit=400)}</td>"
        f'<td class="mono">{free_text(item.dedup_key, limit=200)}</td>'
        f"<td>{ist(item.opened_at)}</td><td>{ist(item.last_seen_at)}</td></tr>"
        for item in alerts.open
    )
    table = (
        "<table><thead><tr><th>Severity</th><th>Trigger</th><th>Title</th><th>Key</th>"
        f"<th>Opened</th><th>Last seen</th></tr></thead><tbody>{rows}</tbody></table>"
        if alerts.open
        else '<p class="muted">no open alert condition</p>'
    )
    return (
        f'<div class="panel" id="alerts"><h2>Alerts <span class="muted">{len(alerts.open)} open'
        f"</span></h2><p>{provider} · {tick}</p>{table}</div>"
    )


# ── backups ────────────────────────────────────────────────────────────────────────────────


def _backup_row(stream: BackupStreamOut) -> str:
    line = stream.line
    state = MISSING if line is None else _state(line.state.value)
    last_run = MISSING
    if line is not None and line.last_started_at is not None:
        last_run = f"{ist(line.last_started_at)} {_state(line.last_state)}"
    last_success = None if line is None else line.last_success_at
    newest = stream.newest
    if newest is None:
        file_cells = (
            f'<td colspan="4" class="muted">{"unreadable" if stream.error else "none"}</td>'
        )
    else:
        detail = []
        if newest.sha256 is not None:
            detail.append(f"sha256 {esc(newest.sha256[:16])}…")
        if newest.entries is not None:
            detail.append(f"{newest.entries} entries")
        file_cells = (
            f'<td class="mono">{esc(newest.name)}</td>'
            f'<td class="num">{human_bytes(newest.bytes)}</td>'
            f"<td>{ist(newest.written_at)} ({age(newest.age_seconds)})</td>"
            f'<td class="mono">{" · ".join(detail) or MISSING}</td>'
        )
    error = "" if stream.error is None else f"<pre>{free_text(stream.error, limit=400)}</pre>"
    return (
        f'<tr data-backup="{esc(stream.job)}"><td class="mono">{esc(stream.job)}</td>'
        f"<td>{state}</td><td>{last_run}</td><td>{ist(last_success)}</td>{file_cells}"
        f'<td class="num">{stream.files}</td><td>{error or MISSING}</td></tr>'
    )


def _backups_panel(backups: BackupsOut) -> str:
    root = f'<span class="mono">{esc(backups.backup_root)}</span>'
    visibility = (
        ""
        if backups.root_visible
        else " — <strong>not visible to this process</strong> (not mounted here?), so the files "
        "below cannot be listed; the job lines are still from job_run"
    )
    return (
        '<div class="panel" id="backups"><h2>Backups</h2>'
        f"<p>BACKUP_ROOT {root}{visibility}</p>"
        "<table><thead><tr><th>Job</th><th>State</th><th>Last run</th><th>Last success</th>"
        "<th>Newest file</th><th>Size</th><th>Written</th><th>Detail</th><th>Files</th>"
        "<th>Error</th></tr></thead><tbody>"
        f"{_backup_row(backups.postgres)}{_backup_row(backups.l0)}</tbody></table>"
        f'<p id="restore-drill">Restore drill: {free_text(backups.restore_drill, limit=400)}</p>'
        "</div>"
    )


# ── pages ──────────────────────────────────────────────────────────────────────────────────


def render_ops(
    *,
    health: HealthOut,
    lines: Sequence[JobLineOut],
    alerts: AlertsOut,
    backups: BackupsOut,
    as_of: datetime,
) -> str:
    """The Operations page: health banner, jobs (worst first), alerts and backups."""
    body = (
        "<h1>Operations</h1>"
        + _health_banner(health)
        + render_jobs_table(lines, as_of=as_of)
        + _alerts_panel(alerts)
        + _backups_panel(backups)
    )
    return _page("Operations", "ops", body, as_of=as_of)


def render_job_runs(line: JobLineOut, runs: JobRunsOut) -> str:
    """One job's drill-down: its Ops line and its newest `job_run` rows, errors in full (masked)."""
    rows = "".join(
        f'<tr data-run="{esc(run.run_id)}"><td>{ist(run.started_at)}</td>'
        f"<td>{ist(run.finished_at)}</td><td>{_state(run.state)}</td>"
        f'<td class="num">{seconds(run.duration_seconds)}</td>'
        f'<td class="mono">{esc(run.instance)}</td><td><pre>{free_text(run.error)}</pre></td></tr>'
        for run in runs.runs
    )
    body = (
        f'<p><a href="/console/ops">← Operations</a></p><h1 class="mono">{esc(line.name)}</h1>'
        f'<p>{_state(line.state.value)} · cron <span class="mono">{esc(line.cron)}</span> · '
        f"budget {seconds(line.timeout_seconds)} · next fire {ist(line.next_fire_at)} · "
        f"due since {ist(line.due_since)} · last success {ist(line.last_success_at)}</p>"
        f"<h2>Newest {len(runs.runs)} of at most {runs.limit} attempts</h2>"
        + (
            "<table><thead><tr><th>Started</th><th>Finished</th><th>State</th><th>Duration</th>"
            f"<th>Instance</th><th>Error</th></tr></thead><tbody>{rows}</tbody></table>"
            if runs.runs
            else '<p class="muted">no job_run row: this job has never been fired</p>'
        )
    )
    return _page(line.name, "ops", body, as_of=runs.as_of)


def render_not_built(slug: str, label: str, *, as_of: datetime) -> str:
    """A planned page that a later M18 task builds."""
    body = (
        f'<h1>{esc(label)}</h1><p class="muted">Not built yet — planned for a later M18 task. '
        'The JSON surface under <span class="mono">/status/</span> already answers.</p>'
    )
    return _page(label, slug, body, as_of=as_of)


def render_not_found(message: str, *, as_of: datetime) -> str:
    """A 404 page in the console's own layout."""
    body = f"<h1>Not found</h1><p>{free_text(message, limit=400)}</p>"
    return _page("Not found", "", body, as_of=as_of)
