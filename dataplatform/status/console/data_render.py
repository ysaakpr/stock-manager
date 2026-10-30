"""M18.2: the Data page and the ISIN lookup as pure functions of the wire models.

The same discipline as `pages.py`: models in, HTML out, no I/O and no clock. Each panel renders
exactly the model its `/status/*` endpoint serves (`SyncStatusOut`, `SourcesOut`, `GapsOut`,
`QualityOut`, `QuarantineOut`) or the new read models of `models_data`, so a panel cannot show a
state its endpoint does not. Every free-text field — an exception string, a gap detail, a flag's
JSON, an exchange's purpose text — goes through `render.free_text`, every instant through
`render.ist`.

`pages._page` and `pages._state` are used as they are: they are the shell's layout and its state
badge, and a second copy of either would let the Data page drift from the Ops page.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from urllib.parse import urlencode

from dataplatform.status.console.pages import (
    ACCEPTED,
    _page,
    _state,
    deviation_attrs,
    deviation_note,
)
from dataplatform.status.console.render import MISSING, esc, free_text, human_bytes, ist
from dataplatform.status.models import (
    CheckCountOut,
    GapEntryOut,
    GapsOut,
    QualityFlagOut,
    QualityOut,
    QuarantineOut,
    SourcesOut,
    SourceStatusOut,
    SyncRowOut,
    SyncStatusOut,
)
from dataplatform.status.models_data import (
    ArchiveListOut,
    IsinLookupOut,
    SymbolSearchOut,
)
from dataplatform.status.sync_state import SyncState

__all__ = [
    "DataPanels",
    "Rag",
    "render_data",
    "render_isin_page",
    "render_unreadable",
    "source_health",
    "sync_rag",
]

#: Free text in a table cell is cut shorter than it would be in a drill-down.
_CELL_TEXT: Final = 240


class Rag:
    """The colours a state renders in on this page. GREY is the owner's pause (D29): not red."""

    RED: Final = "red"
    AMBER: Final = "amber"
    GREEN: Final = "green"
    GREY: Final = "grey"


#: Red/amber/green by state. A pipeline step in flight is amber: not published, not broken.
_SYNC_RAG: Final[dict[SyncState, str]] = {
    SyncState.PUBLISHED: Rag.GREEN,
    SyncState.FAILED: Rag.RED,
    SyncState.PENDING: Rag.AMBER,
    SyncState.FETCHED: Rag.AMBER,
    SyncState.VALIDATED: Rag.AMBER,
    SyncState.NORMALIZED: Rag.AMBER,
}

#: Page-local colours (base.html is the shell's; the Data page adds only these three).
_STYLE: Final = (
    "<style>"
    ".rag { font-weight: 600; padding: 0 0.3rem; border-radius: 3px; white-space: nowrap; }"
    ".rag-red { background: #ffe3e3; color: #a61b1b; }"
    ".rag-amber { background: #fffbea; color: #8d2b0b; }"
    ".rag-green { background: #e3f9e5; color: #0e5814; }"
    ".rag-grey { background: #e4e7eb; color: #3e4c59; }"
    "form.inline { display: inline; margin-right: 1rem; }"
    "h3 { margin: 0.8rem 0 0.2rem; font-size: 0.95rem; }"
    "</style>"
)


def sync_rag(state: SyncState, *, expects_data: bool | None) -> str:
    """A sync_state row's colour. A GAP is green only on a day the calendar says was shut.

    A GAP filed on a trading day is a real miss dressed as a holiday (the gap report calls it
    unexplained), so it is red; with no calendar verdict it is amber — unknown is not fine.
    """
    if state is SyncState.GAP:
        if expects_data is None:
            return Rag.AMBER
        return Rag.RED if expects_data else Rag.GREEN
    return _SYNC_RAG[state]


def source_health(line: SourceStatusOut) -> tuple[str, str]:
    """A `/status/sources` line's one-word verdict and colour, from its own fields only.

    An unhealthy line an accepted deviation covers is `ACCEPTED`, amber — a known, agreed failure,
    not news. Its `healthy` is still false; only the colour and the word change.
    """
    if line.declined is not None:
        return "DECLINED", Rag.GREEN
    if line.paused is not None:
        return "PAUSED", Rag.GREY  # the owner's pause (D29): not requested, not red, not fresh
    if not line.healthy and line.deviation is not None and line.deviation.accepted:
        return ACCEPTED, Rag.AMBER
    if line.failure_streak > 0:
        return "FAILING", Rag.RED
    if line.last_success_date is None:
        return "NO_SUCCESS", Rag.RED
    if line.overdue:
        return "OVERDUE", Rag.RED
    if not line.healthy:
        return "STUCK", Rag.AMBER
    return "OK", Rag.GREEN


#: An ISO-8601 instant with an offset, as `datetime.isoformat()` writes one into a message.
_INSTANT: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)")


def _instants_in_ist(text: str) -> str:
    """Rewrite every offset-bearing ISO instant in a message as IST, labelled.

    Gap details and error strings are written by other modules with `isoformat()`; shown as-is
    they would be the one unlabelled timestamp on the page. Only instants with an offset are
    rewritten — a naive one names no zone, and converting it would be a guess.
    """

    def to_ist(match: re.Match[str]) -> str:
        try:
            return ist(datetime.fromisoformat(match.group(0)))
        except ValueError:
            return match.group(0)

    return _INSTANT.sub(to_ist, text)


def _text(value: str | None, *, limit: int = 2000) -> str:
    """Free text as shown on this page: instants in IST, then masked, bounded and escaped."""
    return free_text(None if value is None else _instants_in_ist(value), limit=limit)


def _rag(label: object, colour: str) -> str:
    text = esc(label)
    return f'<span class="rag rag-{colour}">{text}</span>'


def _num(value: Decimal | None) -> str:
    """A Decimal as written, without float formatting or trailing zeros."""
    if value is None:
        return MISSING
    text = f"{value.normalize():f}"
    return esc(text)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _opt(value: object | None) -> str:
    return MISSING if value is None else esc(value)


def _until(day: date | None) -> str:
    return "current" if day is None else esc(day.isoformat())


def _isin_link(isin: str | None) -> str:
    if isin is None:
        return MISSING
    query = urlencode({"q": isin})
    return f'<a class="mono" href="/console/data/isin?{esc(query)}">{esc(isin)}</a>'


def render_unreadable(panel: str, title: str, error: str) -> str:
    """A panel whose read failed: says so, with the masked reason, instead of blanking the page."""
    return (
        f'<div class="panel" id="{esc(panel)}" data-unreadable="true"><h2>{esc(title)}</h2>'
        f'<div class="banner bad">unreadable: <span class="mono">{_text(error, limit=600)}'
        "</span></div></div>"
    )


# ── sync board ─────────────────────────────────────────────────────────────────────────────


def _sync_row(row: SyncRowOut, *, expects_data: bool | None) -> str:
    source = esc(row.source)
    colour = sync_rag(row.state, expects_data=expects_data)
    return (
        f'<tr data-sync-source="{source}" data-state="{esc(row.state.value)}" '
        f'data-rag="{colour}"><td class="mono">{source}</td>'
        f"<td>{_rag(row.state.value, colour)}</td>"
        f'<td class="num">{row.attempts}</td><td>{"yes" if row.retryable else "no"}</td>'
        f"<td>{ist(row.first_attempt_at)}</td><td>{ist(row.updated_at)}</td>"
        f'<td class="mono">{_text(row.l0_path, limit=_CELL_TEXT)}</td>'
        f"<td><pre>{_text(row.last_error, limit=_CELL_TEXT)}</pre></td></tr>"
    )


def render_sync_board(sync: SyncStatusOut, *, default_date: bool, gaps_range: str) -> str:
    """Every source's sync_state row for one date, coloured by state, with a date picker."""
    day = sync.date.isoformat()
    if sync.day_kind is None:
        kind = "outside the trading calendar's coverage"
    else:
        expects = "data expected" if sync.expects_data else "no data owed"
        kind = f"{esc(sync.day_kind)}, {expects}"
    note = " (the latest trading session, by default)" if default_date else ""
    counts: dict[str, int] = {}
    for row in sync.rows:
        colour = sync_rag(row.state, expects_data=sync.expects_data)
        counts[colour] = counts.get(colour, 0) + 1
    summary = " · ".join(
        f"{_rag(colour, colour)} {counts[colour]}"
        for colour in (Rag.RED, Rag.AMBER, Rag.GREEN)
        if colour in counts
    )
    rows = "".join(_sync_row(row, expects_data=sync.expects_data) for row in sync.rows)
    table = (
        "<table><thead><tr><th>Source</th><th>State</th><th>Attempts</th><th>Retryable</th>"
        "<th>First attempt</th><th>Updated</th><th>L0 path</th><th>Last error</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if sync.rows
        else f'<p class="muted">no sync_state row for {esc(day)}: nothing has been attempted</p>'
    )
    return (
        f'<div class="panel" id="sync" data-date="{esc(day)}">'
        f'<h2>Sync board — {esc(day)}{note} <span class="muted">{kind} · '
        f"{len(sync.rows)} rows</span></h2>"
        '<form class="inline" method="get" action="/console/data">'
        f'<label>Logical date <input type="date" name="date" value="{esc(day)}"></label>'
        f"{gaps_range}"
        ' <button type="submit">Show</button></form>'
        f"<p>{summary or MISSING} · the same rows as "
        f'<a href="/status/sync?{esc(urlencode({"date": day}))}">/status/sync</a>; '
        "the trading verdict is asked per dataset there</p>"
        f"{table}</div>"
    )


# ── sources ────────────────────────────────────────────────────────────────────────────────


def _lag(line: SourceStatusOut) -> str:
    if line.lag_sessions is None:
        sessions = "unmeasurable" if line.last_success_date is not None else MISSING
    else:
        sessions = str(line.lag_sessions)
    budget = "" if line.max_lag_sessions is None else f" / {line.max_lag_sessions}"
    return f"{esc(sessions)}{budget}"


def _source_row(line: SourceStatusOut) -> str:
    verdict, colour = source_health(line)
    source = esc(line.source)
    # `last_error` is the newest failure ever recorded; once the source has recovered (no streak)
    # it is history, and shown beside an OK it read as a live fault. The date stays in its column.
    error = _text(line.last_error, limit=_CELL_TEXT) if line.failure_streak > 0 else MISSING
    if line.declined is not None:
        error = f"declined: {_text(line.declined.reason, limit=_CELL_TEXT)}"
    if line.paused is not None:
        error = (
            f"paused since {esc(line.paused.since.isoformat())} ({esc(line.paused.decision)}): "
            f"{_text(line.paused.reason, limit=_CELL_TEXT)}"
        )
    return (
        f'<tr data-source="{source}" data-health="{esc(verdict)}" '
        f'data-healthy="{"true" if line.healthy else "false"}"{deviation_attrs(line.deviation)}>'
        f'<td class="mono">{source}</td><td>{_rag(verdict, colour)}'
        f"{deviation_note(line.deviation)}</td>"
        f"<td>{_opt(line.last_success_date)}</td><td>{ist(line.last_success_at)}</td>"
        f'<td class="num">{_lag(line)}</td><td class="num">{_opt(line.lag_days)}</td>'
        f'<td class="num">{line.failure_streak}</td><td>{_opt(line.last_failure_date)}</td>'
        f"<td><pre>{error}</pre></td></tr>"
    )


def render_sources(sources: SourcesOut, ordered: Sequence[SourceStatusOut]) -> str:
    """`/status/sources`, worst first: last success (IST), lag, failure streak.

    The heading counts errors and accepted deviations apart, from the lines shown.
    """
    unhealthy = [line for line in ordered if not line.healthy]
    paused = sum(1 for line in unhealthy if line.paused is not None)
    accepted = sum(
        1
        for line in unhealthy
        if line.paused is None and line.deviation is not None and line.deviation.accepted
    )
    errors = len(unhealthy) - accepted - paused
    rows = "".join(_source_row(line) for line in ordered)
    table = (
        "<table><thead><tr><th>Source</th><th>Health</th><th>Last success date</th>"
        "<th>Last success</th><th>Lag (sessions / budget)</th><th>Lag (days)</th>"
        "<th>Failure streak</th><th>Last failure</th><th>Last error</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if ordered
        else '<p class="muted">no source has a sync_state row yet</p>'
    )
    return (
        f'<div class="panel" id="sources" data-errors="{errors}" data-accepted="{accepted}" '
        f'data-paused="{paused}">'
        f'<h2>Sources <span class="muted">{len(ordered)} recorded, {len(unhealthy)} not healthy: '
        f"{errors} error{'' if errors == 1 else 's'}, {accepted} accepted, {paused} paused · lag "
        "measured against "
        f"{esc(sources.as_of.isoformat())}</span></h2>{table}</div>"
    )


# ── gaps ───────────────────────────────────────────────────────────────────────────────────


def _gap_row(entry: GapEntryOut) -> str:
    key = f"{entry.source}|{entry.date.isoformat()}"
    state = MISSING if entry.state is None else esc(entry.state.value)
    accepted = entry.deviation is not None and entry.deviation.accepted
    reason = (
        f'{_rag(ACCEPTED, Rag.AMBER)} <span class="muted">{esc(entry.reason.value)}</span>'
        if accepted
        else _rag(entry.reason.value, Rag.RED)
    )
    return (
        f'<tr data-gap="{esc(key)}" data-reason="{esc(entry.reason.value)}"'
        f"{deviation_attrs(entry.deviation)}>"
        f"<td>{esc(entry.date.isoformat())}</td><td>{reason}{deviation_note(entry.deviation)}</td>"
        f"<td>{esc(entry.day_kind)}</td><td>{state}</td>"
        f'<td class="num">{entry.attempts}</td>'
        f"<td>{_text(entry.detail, limit=_CELL_TEXT)}</td>"
        f"<td><pre>{_text(entry.last_error, limit=_CELL_TEXT)}</pre></td></tr>"
    )


_NO_GAPS: Final = '<p class="muted">no unexplained pair in the range</p>'


def render_gaps(gaps: GapsOut) -> str:
    """`/status/gaps` over a range: the accounting, then every unexplained pair by source."""
    start, end = gaps.from_date.isoformat(), gaps.to_date.isoformat()
    by_source: dict[str, list[GapEntryOut]] = {}
    for entry in gaps.unexplained:
        by_source.setdefault(entry.source, []).append(entry)
    groups = "".join(
        f'<h3 class="mono" data-gap-source="{esc(source)}">{esc(source)} '
        f'<span class="muted">{len(entries)}</span></h3>'
        "<table><thead><tr><th>Date</th><th>Reason</th><th>Day</th><th>State</th>"
        "<th>Attempts</th><th>Detail</th><th>Last error</th></tr></thead><tbody>"
        + "".join(_gap_row(entry) for entry in entries)
        + "</tbody></table>"
        for source, entries in sorted(by_source.items())
    )
    reasons = ", ".join(f"{esc(c.reason.value)} {c.count}" for c in gaps.counts) or MISSING
    errors = gaps.unexplained_total - gaps.accepted_total
    if gaps.fully_explained:
        verdict = _rag("fully explained", Rag.GREEN)
    else:
        # Errors and accepted deviations apart: "22 unexplained, 13 accepted", never one number
        # that the known ones keep high. Both stay unexplained to the M1 gate (`fully_explained`).
        parts = [_rag(f"{errors} unexplained", Rag.RED)] if errors else []
        if gaps.accepted_total:
            parts.append(_rag(f"{gaps.accepted_total} accepted", Rag.AMBER))
        verdict = " ".join(parts)
    truncated = (
        f" — showing the first {len(gaps.unexplained)}"
        if gaps.unexplained_total > len(gaps.unexplained)
        else ""
    )
    examined = ", ".join(esc(source) for source in gaps.sources) or "none tracked"
    return (
        f'<div class="panel" id="gaps" data-from="{esc(start)}" data-to="{esc(end)}" '
        f'data-errors="{errors}" data-accepted="{gaps.accepted_total}">'
        f'<h2>Gaps {esc(start)} … {esc(end)} <span class="muted">{verdict}{truncated}</span></h2>'
        f"<p>{gaps.pairs_examined} pairs examined over sources {examined} · "
        f"{gaps.complete} complete · {gaps.explained_total} explained · "
        f"{gaps.l1_unchecked} PUBLISHED but L1 unchecked · by reason: {reasons} · "
        f'<a href="/status/gaps?{esc(urlencode({"from": start, "to": end}))}">/status/gaps</a>'
        "</p>"
        f"{groups or _NO_GAPS}</div>"
    )


# ── quality ────────────────────────────────────────────────────────────────────────────────


def _check_count(check: CheckCountOut) -> str:
    """The open count, with the accepted part said apart: "194 · 31 accepted"."""
    if not check.accepted:
        return str(check.count)
    return f'{check.count} <span class="muted">· {check.accepted} accepted</span>'


def _flag_severity(flag: QualityFlagOut) -> str:
    if flag.deviation is not None and flag.deviation.accepted:
        return f'{_rag(ACCEPTED, Rag.AMBER)} <span class="muted">{esc(flag.severity.value)}</span>'
    return esc(flag.severity.value)


def _check_severity(check: CheckCountOut) -> str:
    if check.deviation is not None and check.deviation.accepted:
        return f'{_rag(ACCEPTED, Rag.AMBER)} <span class="muted">{esc(check.severity.value)}</span>'
    return esc(check.severity.value)


def render_quality(quality: QualityOut) -> str:
    """Open D7 flags: totals by severity and by check, then the newest flags."""
    severities = (
        " · ".join(f"{esc(c.severity.value)} {c.count}" for c in quality.counts) or "none open"
    )
    checks = "".join(
        f'<tr data-check="{esc(c.check_name)}"{deviation_attrs(c.deviation)}>'
        f'<td class="mono">{esc(c.check_name)}{deviation_note(c.deviation)}</td>'
        f"<td>{_check_severity(c)}</td>"
        f'<td class="num">{_check_count(c)}</td>'
        f"<td>{esc(c.first_date.isoformat())}</td><td>{esc(c.last_date.isoformat())}</td>"
        f'<td class="num">{c.raised_today}</td><td class="num">{c.raised_last_7_days}</td></tr>'
        for c in quality.by_check
    )
    flags = "".join(
        f'<tr data-flag="{flag.id}" data-severity="{esc(flag.severity.value)}"'
        f"{deviation_attrs(flag.deviation)}>"
        f'<td>{esc(flag.date.isoformat())}</td><td class="mono">{esc(flag.check_name)}</td>'
        f"<td>{_flag_severity(flag)}{deviation_note(flag.deviation)}</td>"
        f"<td>{_isin_link(flag.isin)}</td>"
        f'<td class="mono">{_opt(flag.source)}</td><td class="num">{_num(flag.observed_value)}'
        f'</td><td class="num">{_num(flag.threshold)}</td>'
        f"<td><pre>{_text(_json(flag.detail), limit=_CELL_TEXT)}</pre></td>"
        f"<td>{ist(flag.raised_at)}</td></tr>"
        for flag in quality.flags
    )
    by_check = (
        "<table><thead><tr><th>Check</th><th>Severity</th><th>Open</th><th>First date</th>"
        "<th>Last date</th><th>Raised today</th><th>Last 7 days</th></tr></thead>"
        f"<tbody>{checks}</tbody></table>"
        if quality.by_check
        else ""
    )
    newest = (
        f"<h3>Newest {len(quality.flags)} of {quality.open_total} (cap {quality.limit})</h3>"
        "<table><thead><tr><th>Date</th><th>Check</th><th>Severity</th><th>ISIN</th>"
        "<th>Source</th><th>Observed</th><th>Threshold</th><th>Detail</th><th>Raised</th>"
        f"</tr></thead><tbody>{flags}</tbody></table>"
        if quality.flags
        else '<p class="muted">no open quality flag</p>'
    )
    return (
        f'<div class="panel" id="quality" data-open="{quality.open_total}">'
        f'<h2>Quality flags <span class="muted">{quality.open_total} open · {severities}</span>'
        f"</h2>{by_check}{newest}</div>"
    )


# ── quarantine ─────────────────────────────────────────────────────────────────────────────

_QUARANTINE_ROWS: Final = 30


def render_quarantine(quarantine: QuarantineOut) -> str:
    """What L1 refused over the range: totals by reason, the step changes, the newest sessions."""
    start, end = quarantine.from_date.isoformat(), quarantine.to_date.isoformat()
    totals = (
        " · ".join(f"{esc(reason)} {rows}" for reason, rows in quarantine.totals.items()) or MISSING
    )
    steps = "".join(
        f'<tr data-step="{esc(s.trade_date.isoformat())}"><td>{esc(s.trade_date.isoformat())}'
        f"</td><td>{esc(s.exchange)}</td><td>{esc(s.reason)}</td>"
        f'<td class="num">{s.rows}</td><td class="num">{_num(s.baseline)}</td>'
        f'<td class="num">{_num(s.multiple)}</td><td>{_text(s.detail, limit=_CELL_TEXT)}'
        "</td></tr>"
        for s in quarantine.steps
    )
    counts = "".join(
        f"<tr><td>{esc(c.trade_date.isoformat())}</td><td>{esc(c.exchange)}</td>"
        f'<td>{esc(c.reason)}</td><td class="num">{c.rows}</td></tr>'
        for c in quarantine.counts[:_QUARANTINE_ROWS]
    )
    looked = (
        f"{quarantine.rows} rows over {quarantine.partitions_read} partitions"
        if quarantine.partitions_read
        else "no quarantine partition in the range — nothing was looked at, not nothing found"
    )
    return (
        f'<div class="panel" id="quarantine" data-rows="{quarantine.rows}">'
        f'<h2>Quarantine {esc(start)} … {esc(end)} <span class="muted">{looked}</span></h2>'
        f"<p>by reason: {totals} · "
        f'<a href="/status/quarantine?{esc(urlencode({"from": start, "to": end}))}">'
        "/status/quarantine</a></p>"
        + (
            "<h3>Step changes</h3><table><thead><tr><th>Session</th><th>Exchange</th>"
            "<th>Reason</th><th>Rows</th><th>Baseline</th><th>Multiple</th><th>Detail</th>"
            f"</tr></thead><tbody>{steps}</tbody></table>"
            if quarantine.steps
            else '<p class="muted">no step change in the range</p>'
        )
        + (
            f"<h3>Newest {min(len(quarantine.counts), _QUARANTINE_ROWS)} sessions</h3>"
            "<table><thead><tr><th>Session</th><th>Exchange</th><th>Reason</th><th>Rows</th>"
            f"</tr></thead><tbody>{counts}</tbody></table>"
            if quarantine.counts
            else ""
        )
        + "</div>"
    )


# ── archives ───────────────────────────────────────────────────────────────────────────────


def render_archives(archives: ArchiveListOut) -> str:
    """The newest published bundles, each linking to its `/archives?date=` manifest."""
    rows = "".join(
        f'<tr data-archive="{esc(b.logical_date.isoformat())}">'
        f'<td><a href="/archives?{esc(urlencode({"date": b.logical_date.isoformat()}))}">'
        f"{esc(b.logical_date.isoformat())}</a></td>"
        f'<td class="num">{b.file_count}</td><td class="num">{human_bytes(b.total_bytes)}</td>'
        f'<td>{ist(b.published_at)}</td><td class="mono">{esc(b.bundle_path)}</td>'
        f'<td class="mono">{esc(b.manifest_sha256[:16])}…</td>'
        f"<td>{esc(b.schema_version)}</td></tr>"
        for b in archives.bundles
    )
    table = (
        "<table><thead><tr><th>Date</th><th>Files</th><th>Size</th><th>Published</th>"
        "<th>Path</th><th>Manifest sha256</th><th>Schema</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if archives.bundles
        else '<p class="muted">no archive bundle has been published</p>'
    )
    return (
        f'<div class="panel" id="archives"><h2>Archives <span class="muted">newest '
        f"{len(archives.bundles)} of {archives.total} · local and personal use only; files "
        'download through <span class="mono">/archives/download</span></span></h2>'
        f"{table}</div>"
    )


# ── the page ───────────────────────────────────────────────────────────────────────────────


def _lookup_form(value: str = "") -> str:
    return (
        '<form class="inline" method="get" action="/console/data/isin">'
        f'<label>ISIN or symbol <input type="text" name="q" value="{esc(value)}" '
        'maxlength="40"></label> <button type="submit">Look up</button></form>'
    )


def gaps_range_inputs(start: date, end: date) -> str:
    """The gaps range as form inputs, carried by the sync board's date picker too."""
    return (
        f' <label>Gaps from <input type="date" name="from" value="{esc(start.isoformat())}">'
        f'</label> <label>to <input type="date" name="to" value="{esc(end.isoformat())}"></label>'
    )


class DataPanels:
    """The page's panels, each already rendered (or rendered as unreadable) by the route."""

    __slots__ = ("archives", "gaps", "quality", "quarantine", "sources", "sync")

    def __init__(
        self, *, sync: str, sources: str, gaps: str, quality: str, quarantine: str, archives: str
    ) -> None:
        self.sync = sync
        self.sources = sources
        self.gaps = gaps
        self.quality = quality
        self.quarantine = quarantine
        self.archives = archives


def render_data(panels: DataPanels, *, as_of: datetime) -> str:
    """The Data page: sync board, sources, gaps, quality, quarantine, archives, ISIN lookup."""
    body = (
        _STYLE
        + "<h1>Data</h1>"
        + f'<div class="panel" id="lookup"><h2>ISIN lookup</h2>{_lookup_form()}</div>'
        + panels.sync
        + panels.sources
        + panels.gaps
        + panels.quality
        + panels.quarantine
        + panels.archives
    )
    return _page("Data", "data", body, as_of=as_of)


# ── ISIN lookup ────────────────────────────────────────────────────────────────────────────


def _lookup_body(found: IsinLookupOut) -> str:
    security = found.security
    if security is None:
        master = (
            '<p class="banner warn">not in security_master — a retired ISIN is held only as '
            "lineage, below</p>"
        )
    else:
        master = (
            '<table id="master"><tbody>'
            f"<tr><th>Name</th><td>{esc(security.name)}</td></tr>"
            f"<tr><th>Status</th><td>{_state(security.status)}</td></tr>"
            f"<tr><th>Primary exchange</th><td>{esc(security.primary_exchange)}</td></tr>"
            f"<tr><th>Face value (INR)</th><td>{_num(security.face_value_inr)}</td></tr>"
            f"<tr><th>First seen</th><td>{esc(security.first_seen_date.isoformat())}</td></tr>"
            f"<tr><th>Last seen</th><td>{_opt(security.last_seen_date)}</td></tr>"
            f"<tr><th>Registered by</th><td>{_opt(security.registered_by)}</td></tr>"
            f"<tr><th>Updated</th><td>{ist(security.updated_at)}</td></tr>"
            "</tbody></table>"
        )
    symbols = "".join(
        f'<tr data-symbol="{esc(w.symbol)}"><td>{esc(w.exchange)}</td>'
        f'<td class="mono">{esc(w.symbol)}</td><td>{_opt(w.series)}</td>'
        f"<td>{esc(w.valid_from.isoformat())}</td><td>{_until(w.valid_to)}</td>"
        f'<td class="mono">{esc(w.source)}</td></tr>'
        for w in found.symbols
    )
    lineage = "".join(
        f'<tr data-lineage="{esc(e.predecessor_isin)}|{esc(e.successor_isin)}">'
        f"<td>{_isin_link(e.predecessor_isin)}</td><td>{_isin_link(e.successor_isin)}</td>"
        f"<td>{esc(e.effective_date.isoformat())}</td><td>{esc(e.detected_by)}</td>"
        f"<td>{esc(e.confidence)}</td><td>{_opt(e.corroborating_action)}</td>"
        f'<td class="num">{e.gap_sessions}</td><td class="mono">{esc(e.symbol_at_change)}</td></tr>'
        for e in found.lineage
    )
    factor_on = {f.ex_date: f for f in found.factors}
    actions = ""
    for a in found.actions:
        factor = factor_on.get(a.ex_date)
        cells = (
            f'<td class="num">{_num(factor.price_factor)}</td>'
            f'<td class="num">{_num(factor.cum_price_factor)}</td>'
            if factor is not None
            else f'<td colspan="2" class="muted">{MISSING}</td>'
        )
        filed = (
            ""
            if a.filed_against_isin is None
            else f" (filed against {_isin_link(a.filed_against_isin)})"
        )
        actions += (
            f'<tr data-ca="{a.id}" data-type="{esc(a.action_type)}">'
            f"<td>{esc(a.ex_date.isoformat())}</td><td>{esc(a.action_type)}{filed}</td>"
            f"<td><pre>{_text(_json(a.ratio_terms), limit=_CELL_TEXT)}</pre></td>"
            f'<td class="num">{_num(a.dividend_amount_inr)}</td>'
            f"<td>{_opt(a.record_date)}</td><td>{esc(a.knowable_date.isoformat())}</td>"
            f'<td class="mono">{esc(a.source)}</td><td>{"yes" if a.reconciled else "no"}</td>'
            f"{cells}<td><pre>{_text(a.raw_text, limit=_CELL_TEXT)}</pre></td></tr>"
        )
    factors = "".join(
        f'<tr data-factor="{esc(f.ex_date.isoformat())}"><td>{esc(f.ex_date.isoformat())}</td>'
        f'<td class="num">{_num(f.price_factor)}</td><td class="num">{_num(f.qty_factor)}</td>'
        f'<td class="num">{_num(f.cum_price_factor)}</td>'
        f'<td class="num">{_num(f.cum_qty_factor)}</td>'
        f"<td>{_opt(f.corporate_action_id)}</td>"
        f"<td>{'yes' if f.structural_break else 'no'}</td><td>{ist(f.computed_at)}</td></tr>"
        for f in found.factors
    )
    pending = "".join(
        f'<tr data-invalidation="{p.id}"><td>{_text(p.reason, limit=_CELL_TEXT)}</td>'
        f"<td>{_opt(p.from_date) if p.from_date else 'whole series'}</td>"
        f"<td>{ist(p.requested_at)}</td></tr>"
        for p in found.pending_invalidations
    )

    def table(panel: str, title: str, head: str, rows: str, empty: str, count: int) -> str:
        content = (
            f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
            if rows
            else f'<p class="muted">{empty}</p>'
        )
        return (
            f'<div class="panel" id="{panel}"><h2>{title} <span class="muted">{count}</span></h2>'
            f"{content}</div>"
        )

    return (
        f'<div class="panel" id="security"><h2>Security master</h2>{master}</div>'
        + table(
            "symbols",
            "Symbol history",
            "<th>Exchange</th><th>Symbol</th><th>Series</th><th>Valid from</th><th>Valid to</th>"
            "<th>Source</th>",
            symbols,
            "no symbol_history window",
            len(found.symbols),
        )
        + table(
            "lineage",
            "ISIN lineage",
            "<th>Predecessor</th><th>Successor</th><th>Effective</th><th>Detected by</th>"
            "<th>Confidence</th><th>Corroborated by</th><th>Gap sessions</th>"
            "<th>Symbol at change</th>",
            lineage,
            "no lineage edge: this ISIN was never retired into, or out of, another",
            len(found.lineage),
        )
        + table(
            "actions",
            "Corporate actions",
            "<th>Ex-date</th><th>Type</th><th>Terms</th><th>Dividend (INR)</th><th>Record</th>"
            "<th>Knowable</th><th>Source</th><th>Reconciled</th><th>Price factor</th>"
            "<th>Cum. price factor</th><th>Exchange text</th>",
            actions,
            "no corporate action filed under this ISIN",
            len(found.actions),
        )
        + table(
            "factors",
            "Adjustment factor chain",
            "<th>Ex-date</th><th>Price</th><th>Qty</th><th>Cum. price</th><th>Cum. qty</th>"
            "<th>Action id</th><th>Structural break</th><th>Computed</th>",
            factors,
            "no adjustment factor: raw and adjusted prices coincide",
            len(found.factors),
        )
        + table(
            "invalidations",
            "Pending L2 invalidations",
            "<th>Reason</th><th>From</th><th>Requested</th>",
            pending,
            "none: L2 is current for this ISIN's factors",
            len(found.pending_invalidations),
        )
    )


def _search_body(found: SymbolSearchOut) -> str:
    rows = "".join(
        f'<tr data-candidate="{esc(c.isin)}"><td>{_isin_link(c.isin)}</td>'
        f"<td>{esc(c.name)}</td><td>{_state(c.status)}</td><td>{esc(c.exchange)}</td>"
        f'<td class="mono">{esc(c.symbol)}</td><td>{_opt(c.series)}</td>'
        f"<td>{esc(c.valid_from.isoformat())}</td>"
        f"<td>{_until(c.valid_to)}</td></tr>"
        for c in found.candidates
    )
    isins = len({c.isin for c in found.candidates})
    content = (
        "<table><thead><tr><th>ISIN</th><th>Name</th><th>Status</th><th>Exchange</th>"
        "<th>Symbol</th><th>Series</th><th>Valid from</th><th>Valid to</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if rows
        else f'<p class="muted">no symbol_history window has the symbol {esc(found.query)}</p>'
    )
    return (
        f'<div class="panel" id="candidates" data-isins="{isins}"><h2>Candidate ISINs for '
        f'<span class="mono">{esc(found.query)}</span> <span class="muted">{isins} ISINs, '
        f"{len(found.candidates)} windows</span></h2>"
        "<p>A symbol is a display name, not a key: pick the ISIN. Nothing on this page is read "
        "by symbol (invariant #2).</p>"
        f"{content}</div>"
    )


def render_isin_page(
    query: str,
    *,
    lookup: IsinLookupOut | None = None,
    search: SymbolSearchOut | None = None,
    error: str | None = None,
    as_of: datetime,
) -> str:
    """The lookup page: the form, then an ISIN's record, a symbol's candidates, or an error."""
    if lookup is not None:
        name = lookup.security.name if lookup.security is not None else "not in the master"
        result = (
            f'<h2 class="mono" id="isin" data-isin="{esc(lookup.isin)}">{esc(lookup.isin)} '
            f'<span class="muted">{esc(name)}</span></h2>' + _lookup_body(lookup)
        )
    elif search is not None:
        result = _search_body(search)
    elif error is not None:
        result = f'<div class="banner bad">{_text(error, limit=600)}</div>'
    else:
        result = '<p class="muted">Enter an ISIN, or a symbol to list the ISINs it has been.</p>'
    body = (
        _STYLE
        + '<p><a href="/console/data">← Data</a></p><h1>ISIN lookup</h1>'
        + f'<div class="panel" id="lookup">{_lookup_form(query)}</div>'
        + result
    )
    return _page("ISIN lookup", "data", body, as_of=as_of)
