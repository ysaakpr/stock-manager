"""The Journal page's renderers (M18.4): models in, HTML out — no I/O, no clock, no route logic.

Built on the M18.1 shell (`pages._page`): the same layout, navigation and `as of` stamp. Every
free-text field goes through `render.free_text` (mask, bound, escape) even though the query layer
already masked it, every instant through `render.ist`, and every link is a path on this app.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final
from urllib.parse import quote, urlencode

from dataplatform.status.console.pages import _page, _state
from dataplatform.status.console.render import MISSING, esc, free_text, human_bytes, ist
from dataplatform.status.models_journal import (
    CasesOut,
    EvidenceBundleOut,
    EvidenceListOut,
    JournalEntryOut,
    JournalFacetsOut,
    JournalPageOut,
    JournalQuery,
)
from dataplatform.status.queries_journal import PAYLOAD_CHARS

__all__ = [
    "render_cases",
    "render_evidence_bundle",
    "render_evidence_list",
    "render_journal",
    "render_journal_error",
    "search_href",
]

_SLUG: Final = "journal"

#: A rationale in a table row is cut; the opened entry shows it whole (masked).
_CELL_TEXT: Final = 240


def _subnav(current: str) -> str:
    links = (
        ("/console/journal", "Decisions"),
        ("/console/journal/cases", "Cases"),
        ("/console/journal/evidence", "Evidence"),
    )
    return (
        '<p class="subnav">'
        + " · ".join(
            f"<strong>{esc(label)}</strong>" if href == current else f'<a href="{href}">{label}</a>'
            for href, label in links
        )
        + '</p><p class="muted">Read-only: the decision journal is append-only and permanent; '
        "this page only reads it.</p>"
    )


# ── decision search ────────────────────────────────────────────────────────────────────────


def _params(query: JournalQuery, before: int | None) -> dict[str, str]:
    fields: Mapping[str, object | None] = {
        "date_from": query.date_from,
        "date_to": query.date_to,
        "case": query.case_id,
        "manager": query.book,
        "kind": query.decision,
        "before": before,
    }
    return {key: str(value) for key, value in fields.items() if value is not None}


def search_href(query: JournalQuery, *, before: int | None) -> str:
    """The URL of `query`'s page that starts below entry `before` (the first page when None)."""
    params = _params(query, before)
    return "/console/journal" + ("?" + urlencode(params) if params else "")


def _select(name: str, label: str, choices: Sequence[str], chosen: str | None) -> str:
    options = '<option value="">any</option>' + "".join(
        f'<option value="{esc(c)}"{" selected" if c == chosen else ""}>{esc(c)}</option>'
        for c in choices
    )
    return f'<label>{esc(label)} <select name="{name}">{options}</select></label>'


def _form(query: JournalQuery, facets: JournalFacetsOut) -> str:
    def day(name: str, value: object | None) -> str:
        shown = "" if value is None else esc(value)
        return f'<input type="date" name="{name}" value="{shown}" />'

    return (
        '<form method="get" action="/console/journal" id="journal-search" class="panel">'
        f"<label>trading date from {day('date_from', query.date_from)}</label> "
        f"<label>to {day('date_to', query.date_to)}</label> "
        + _select("case", "case", facets.cases, query.case_id)
        + " "
        + _select("manager", "manager (M17 stream)", facets.books, query.book)
        + " "
        + _select("kind", "kind", facets.kinds, query.decision)
        + ' <button type="submit">Search</button> <a href="/console/journal">clear</a></form>'
    )


def _entry_row(entry: JournalEntryOut) -> str:
    kind = _state(entry.decision) + (' <span class="muted">no-op</span>' if entry.no_op else "")
    tokens = MISSING
    if entry.tokens_in is not None or entry.tokens_out is not None:
        tokens = f"{entry.tokens_in or 0} / {entry.tokens_out or 0}"
    cost = esc(MISSING if entry.cost_inr is None else entry.cost_inr)
    evidence = _evidence_link(entry.evidence_ref)
    detail = (
        f"<details><summary>payload</summary>"
        f"<p>recorded {ist(entry.recorded_at)} · model {esc(entry.model or MISSING)} · "
        f"tokens in/out {tokens} · cost ₹ {cost}"
        f" · orders {esc(entry.orders_ref or MISSING)} · evidence {evidence}"
        "</p>"
        f'<pre class="payload">{free_text(entry.payload, limit=PAYLOAD_CHARS)}</pre>'
        f"<p>rationale</p><pre>{free_text(entry.rationale, limit=PAYLOAD_CHARS)}</pre>"
        f'<p>break conditions evaluated</p><pre class="payload">'
        f"{free_text(entry.break_conditions, limit=PAYLOAD_CHARS)}</pre></details>"
    )
    return (
        f'<tr data-entry="{entry.id}" data-decision="{esc(entry.decision)}" '
        f'data-noop="{"1" if entry.no_op else "0"}">'
        f'<td class="num">{entry.id}</td><td>{esc(entry.trading_date)}</td>'
        f"<td>{ist(entry.ts)}</td><td>{_case_link(entry.case_id)}</td>"
        f'<td class="mono">{esc(entry.book or MISSING)}</td><td>{esc(entry.actor)}</td>'
        f'<td>{kind}</td><td class="mono">{esc(entry.isin or MISSING)}</td>'
        f"<td><pre>{free_text(entry.rationale, limit=_CELL_TEXT)}</pre>{detail}</td></tr>"
    )


def _case_link(case_id: str | None) -> str:
    if case_id is None:
        return MISSING
    href = esc("/console/journal?" + urlencode({"case": case_id}))
    return f'<a class="mono" href="{href}">{esc(case_id)}</a>'


def _evidence_link(ref: str | None) -> str:
    if ref is None:
        return MISSING
    href = "/console/journal/evidence?" + urlencode({"ref": ref})
    return f'<a class="mono" href="{esc(href)}">{esc(ref[:19])}…</a>'


def render_journal(page: JournalPageOut, facets: JournalFacetsOut) -> str:
    """The decision search: the form, one page of entries newest first, and the next-page link."""
    query = page.query
    rows = "".join(_entry_row(entry) for entry in page.entries)
    noops = sum(entry.no_op for entry in page.entries)
    if page.entries:
        table = (
            "<table><thead><tr><th>#</th><th>Trading date</th><th>Logged</th><th>Case</th>"
            "<th>Manager</th><th>Actor</th><th>Kind</th><th>ISIN</th><th>Rationale</th>"
            f'</tr></thead><tbody id="journal-entries">{rows}</tbody></table>'
        )
    else:
        table = '<p class="muted" id="journal-entries">no journal entry matches this search</p>'
    pager = []
    if query.before is not None:
        pager.append(f'<a href="{esc(search_href(query, before=None))}">newest</a>')
    if page.next_before is not None:
        href = esc(search_href(query, before=page.next_before))
        pager.append(f'<a id="next-page" href="{href}">older →</a>')
    where = "from the newest entry" if query.before is None else f"below entry #{query.before}"
    body = (
        "<h1>Decision journal</h1>"
        + _subnav("/console/journal")
        + _form(query, facets)
        + f'<h2>{len(page.entries)} entries <span class="muted">{where}, newest first, '
        f"{noops} no-ops · {query.limit} per page</span></h2>"
        + table
        + f'<p class="pager">{" · ".join(pager) or MISSING}</p>'
    )
    return _page("Journal", _SLUG, body, as_of=page.as_of)


def render_journal_error(message: str, *, as_of: datetime) -> str:
    """A search the route refused (a malformed date, an unknown kind), said in the layout."""
    body = (
        "<h1>Decision journal</h1>"
        + _subnav("/console/journal")
        + f'<p class="banner bad">{free_text(message, limit=400)}</p>'
        '<p><a href="/console/journal">start over</a></p>'
    )
    return _page("Journal", _SLUG, body, as_of=as_of)


# ── cases ──────────────────────────────────────────────────────────────────────────────────


def render_cases(cases: CasesOut) -> str:
    """Every case with its lifecycle stage; the case id opens its ratification page."""
    rows = "".join(
        f'<tr data-case="{esc(line.case_id)}" data-stage="{esc(line.state)}">'
        f'<td><a class="mono" href="/cases/{esc(quote(line.case_id, safe=""))}">'
        f"{esc(line.case_id)}</a></td>"
        f"<td>{free_text(line.title, limit=_CELL_TEXT)}</td><td>{_state(line.state)}</td>"
        f"<td>{esc(line.funding_mode)}</td><td>{free_text(line.theme, limit=_CELL_TEXT)}</td>"
        f'<td class="num"><a href="{esc("/console/journal?" + urlencode({"case": line.case_id}))}">'
        f"{line.entries}</a></td><td>{esc(line.last_trading_date or MISSING)}</td>"
        f"<td>{ist(line.created_at)}</td><td>{ist(line.updated_at)}</td></tr>"
        for line in cases.cases
    )
    table = (
        "<table><thead><tr><th>Case</th><th>Title</th><th>Stage</th><th>Funding</th>"
        "<th>Theme</th><th>Journal entries</th><th>Last trading date</th><th>Created</th>"
        f"<th>Updated</th></tr></thead><tbody>{rows}</tbody></table>"
        if cases.cases
        else '<p class="muted">no case is recorded</p>'
    )
    body = (
        "<h1>Cases</h1>"
        + _subnav("/console/journal/cases")
        + '<p class="muted">A case id opens its ratification page '
        '(<span class="mono">/cases/{id}</span>, served by the ratification app, '
        '<span class="mono">analyst.cases.web</span>); the entry count opens its journal.</p>'
        + f"<h2>{len(cases.cases)} cases</h2>{table}"
    )
    return _page("Cases", _SLUG, body, as_of=cases.as_of)


# ── evidence ───────────────────────────────────────────────────────────────────────────────


def render_evidence_list(listing: EvidenceListOut) -> str:
    """The A9 evidence store's bundles, newest first, each opening its content."""
    root = f'<span class="mono">{esc(listing.root)}</span>'
    if not listing.root_visible:
        table = (
            f"<p>{root} is <strong>not visible to this process</strong> (not mounted here?), so "
            "no bundle can be listed — this is not the same as there being none.</p>"
        )
    elif not listing.bundles:
        table = f'<p class="muted">{root} holds no bundle</p>'
    else:
        rows = "".join(
            f'<tr data-bundle="{esc(line.ref)}"><td>{_evidence_link(line.ref)}</td>'
            f'<td class="num">{human_bytes(line.bytes)}</td><td>{ist(line.written_at)}</td>'
            f'<td class="num">{line.entries}</td><td>{ist(line.last_referenced_at)}</td></tr>'
            for line in listing.bundles
        )
        table = (
            f"<p>store {root} · {listing.total} bundles, newest {len(listing.bundles)} shown "
            f"(at most {listing.limit})</p>"
            "<table><thead><tr><th>Bundle</th><th>Size</th><th>Written</th><th>Cited by</th>"
            f"<th>Last cited</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    body = (
        "<h1>Evidence</h1>"
        + _subnav("/console/journal/evidence")
        + '<p class="muted">The content-addressed bundles A9 stores for journal entries: the '
        "exact material a decision was made on. The §5.7 graduation pack "
        '(<span class="mono">generate_pack</span>) is assembled in memory from the journal and '
        "is not persisted anywhere this page could list.</p>" + table
    )
    return _page("Evidence", _SLUG, body, as_of=listing.as_of)


def render_evidence_bundle(bundle: EvidenceBundleOut) -> str:
    """One bundle: its integrity verdict, the entries that cite it, and its content (masked)."""
    if bundle.verified:
        verdict = '<span class="state state-OK">verified</span> sha256 of the bytes matches'
    else:
        verdict = (
            '<span class="state state-FAILED">MISMATCH</span> the stored bytes do not hash to '
            "their name: the file changed after it was written"
        )
    cited = ", ".join(
        f'<a href="/console/journal?{urlencode({"before": entry_id + 1})}">#{entry_id}</a>'
        for entry_id in bundle.referenced_by
    )
    body = (
        '<p><a href="/console/journal/evidence">← Evidence</a></p>'
        f'<h1 class="mono">{esc(bundle.ref)}</h1>'
        f"<p>{verdict} · {human_bytes(bundle.bytes)} · written {ist(bundle.written_at)}</p>"
        f"<p>cited by {cited or 'no journal entry'}</p>"
        f'<pre class="payload" id="bundle">{free_text(bundle.content, limit=PAYLOAD_CHARS)}</pre>'
    )
    return _page(bundle.ref[:19], _SLUG, body, as_of=bundle.as_of)
