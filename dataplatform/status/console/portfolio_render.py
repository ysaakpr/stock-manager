"""The Portfolio page (M18.7) as a pure function of `PortfolioOut`: model in, HTML out.

Money is printed to the paisa from the model's exact `Decimal` (half-up, the way a statement
rounds), with Indian digit grouping; a percentage to two places. An absent figure is `MISSING`,
never a zero. The same rounded values go into each row's ``data-*`` attributes, so a test reads
what the operator reads.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from urllib.parse import quote

from dataplatform.status.console.pages import _page
from dataplatform.status.console.render import MISSING, esc, free_text
from dataplatform.status.models_portfolio import (
    BookChoiceOut,
    BookGlanceOut,
    FundTotalsOut,
    HoldingRowOut,
    PortfolioOut,
    PortfolioStream,
    StagedAction,
)

__all__ = ["REGION_ID", "paisa", "pct", "render_portfolio"]

_SLUG: Final = "portfolio"
_PAISA: Final = Decimal("0.01")
#: The picker's groups, in this order; a kind the roster adds later gets its own group after.
_KIND_LABELS: Final = {
    "MANAGER": "Manager",
    "CONTROL": "Control",
    "STYLE": "Style",
    "BENCH": "Bench",
}


def paisa(value: Decimal | None) -> str:
    """``value`` to the paisa as a plain decimal string (``-1234.50``); `MISSING` if None."""
    if value is None:
        return MISSING
    return str(value.quantize(_PAISA, rounding=ROUND_HALF_UP))


def pct(value: Decimal | None) -> str:
    """A percentage to two places (``3.25``); `MISSING` if None."""
    return paisa(value)


def _group_indian(digits: str) -> str:
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head, *pairs, tail]) if head else ",".join([*pairs, tail])


def _rupees(value: Decimal | None, *, signed: bool = False) -> str:
    """₹ to the paisa with Indian grouping (``₹10,00,000.00``)."""
    if value is None:
        return MISSING
    text = paisa(value)
    negative = text.startswith("-")
    whole, _, fraction = text.lstrip("-").partition(".")
    sign = "-" if negative else ("+" if signed and text.strip("0.") else "")
    return f"{sign}₹{_group_indian(whole)}.{fraction}"


def _percent(value: Decimal | None, *, signed: bool = True) -> str:
    if value is None:
        return MISSING
    text = pct(value)
    sign = "+" if signed and not text.startswith("-") and text.strip("0.") else ""
    return f"{sign}{text} %"


def _tone(value: Decimal | None) -> str:
    if value is None or value == 0:
        return ""
    return ' style="color:#0e5814"' if value > 0 else ' style="color:#a61b1b"'


# ── the book picker and the labels ────────────────────────────────────────────────────────────


def _owner(book: BookChoiceOut) -> str:
    if book.kind == "BENCH" or book.manager is None:
        return esc(book.kind.lower())
    relation = {"MANAGER": "manager", "CONTROL": "control for", "STYLE": "style book of"}
    return f"{esc(relation.get(book.kind, book.kind.lower()))} {esc(book.manager)}"


#: A book id that names a capital variant (``FM-POS-TREND-10L``): the stem and the variant. The
#: picker puts a stem's variants on one line, so a manager's primary and mirror sit side by side.
_VARIANT: Final = re.compile(r"^(?P<stem>.+)-(?P<variant>\d+(?:L|CR))$")
#: Where the picker's htmx swap lands: the whole portfolio region, picker included, so the
#: highlight always matches the book shown.
REGION_ID: Final = "portfolio"


def _whole_rupees(value: Decimal) -> str:
    """₹ to the rupee with Indian grouping (``₹10,01,000``) — the picker's compact NAV."""
    text = str(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    negative = text.startswith("-")
    return f"{'-' if negative else ''}₹{_group_indian(text.lstrip('-'))}"


def _glance(glance: BookGlanceOut | None) -> str:
    if glance is None or glance.nav is None:
        return '<span class="pick-glance muted">not marked</span>'
    change = _percent(glance.daily_change_pct)
    return (
        f'<span class="pick-glance muted">{_whole_rupees(glance.nav)} · '
        f"<span{_tone(glance.daily_change_pct)}>{change}</span></span>"
    )


def _pick(book: BookChoiceOut, label: str, page: PortfolioOut) -> str:
    glance = next((g for g in page.glances if g.book_id == book.book_id), None)
    href = esc(f"/console/portfolio?book={quote(book.book_id, safe='')}")
    current = book.book_id == page.book.book_id
    staged = (
        ' <span class="pick-staged" title="BUY/SELL staged for the next session">●</span>'
        if glance is not None and glance.staged
        else ""
    )
    attrs = (
        f'href="{href}" hx-get="{href}" data-book="{esc(book.book_id)}" '
        f'data-staged="{str(glance is not None and glance.staged).lower()}" '
        f'class="pick{" current" if current else ""}"' + (' aria-current="page"' if current else "")
    )
    return f'<a {attrs}><span class="mono">{esc(label)}</span>{staged}{_glance(glance)}</a>'


def _picker(page: PortfolioOut) -> str:
    """Every book as a one-click link, grouped by kind; a stem's capital variants share a line.

    Each item is a plain GET link (works with JS off, bookmarkable); with htmx it fetches the
    same URL and swaps only the portfolio region, pushing the URL to history.
    """
    kinds = list(_KIND_LABELS) + sorted({b.kind for b in page.books} - set(_KIND_LABELS))
    groups = []
    for kind in kinds:
        books = [b for b in page.books if b.kind == kind]
        if not books:
            continue
        lines: list[tuple[str | None, list[tuple[BookChoiceOut, str]]]] = []
        for book in books:
            match = _VARIANT.match(book.book_id)
            if match is None:
                lines.append((None, [(book, book.book_id)]))
            elif lines and lines[-1][0] == match["stem"]:
                lines[-1][1].append((book, match["variant"]))
            else:
                lines.append((match["stem"], [(book, match["variant"])]))
        items = "".join(
            '<div class="pick-line">'
            + ("" if stem is None else f'<span class="pick-stem mono">{esc(stem)}</span>')
            + "".join(_pick(book, label, page) for book, label in picks)
            + "</div>"
            for stem, picks in lines
        )
        groups.append(
            f'<div class="pick-group" data-kind="{esc(kind)}">'
            f"<h3>{esc(_KIND_LABELS.get(kind, kind))}</h3>{items}</div>"
        )
    return (
        f'<nav id="book-picker" class="panel" aria-label="Books" hx-target="#{REGION_ID}" '
        'hx-swap="outerHTML" hx-push-url="true">' + "".join(groups) + "</nav>"
    )


def _stream_banner(page: PortfolioOut) -> str:
    record = (
        ""
        if page.desk_record is None
        else f' · holdings from the desk record <span class="mono">{esc(page.desk_record)}</span>'
    )
    if page.stream is PortfolioStream.DRY:
        return (
            f'<div class="banner warn" id="stream" data-stream="{esc(page.stream.value)}">'
            f'<strong>DRY RUN</strong> — the dry-run stream <span class="mono">'
            f"{esc(page.stream.value)}</span>{record}. Nothing in it counts (pre-registration §6); "
            "the live stream has no entry yet (it starts at S0).</div>"
        )
    return (
        f'<div class="banner ok" id="stream" data-stream="{esc(page.stream.value)}">'
        f'<strong>LIVE</strong> — the live stream <span class="mono">{esc(page.stream.value)}'
        f"</span>{record}.</div>"
    )


# ── the holdings ───────────────────────────────────────────────────────────────────────────


def _position(row: HoldingRowOut) -> str:
    # A name not held (a staged BUY) has no position: the cell is empty, not a dash or a zero.
    if not row.held:
        return ""
    return _rupees(row.position_day_change, signed=True)


def _change_cells(row: HoldingRowOut) -> str:
    return (
        f'<td class="num"{_tone(row.day_change)}>{_rupees(row.day_change, signed=True)}</td>'
        f'<td class="num"{_tone(row.position_day_change)}>{_position(row)}</td>'
        f'<td class="num"{_tone(row.day_change)}>{_percent(row.day_change_pct)}</td>'
    )


def _row(row: HoldingRowOut) -> str:
    action = "" if row.action is None else row.action.value
    attrs = (
        f'data-holding="{esc(row.isin)}" data-held="{str(row.held).lower()}" '
        f'data-action="{esc(action)}" data-action-quantity="{row.action_quantity or ""}" '
        f'data-quantity="{"" if row.quantity is None else row.quantity}" '
        f'data-cmp="{paisa(row.cmp)}" data-avg="{paisa(row.average_price)}" '
        f'data-day-change="{paisa(row.day_change)}" '
        f'data-position-day-change="{"" if not row.held else paisa(row.position_day_change)}" '
        f'data-day-change-pct="{pct(row.day_change_pct)}" '
        f'data-value="{paisa(row.value)}" data-profit="{paisa(row.profit)}" '
        f'data-profit-pct="{pct(row.profit_pct)}" '
        f'data-suspended-since="{"" if row.suspended_since is None else row.suspended_since}"'
    )
    action_cell = (
        ""
        if row.action is None
        else f'<span class="state state-{esc(action)}">{esc(action)} {row.action_quantity}</span>'
    )
    symbol = esc(row.symbol) if row.symbol else MISSING
    name = f'<td>{symbol}</td><td class="mono">{esc(row.isin)}</td>'
    if row.suspended_since is not None:
        cmp_cells = (
            f"<td><strong>suspended since {row.suspended_since.isoformat()}</strong></td>"
            f'<td class="num">{_rupees(row.average_price)}</td>'
            f'<td colspan="3" class="muted">{free_text(row.note, limit=240)}</td>'
            f'<td class="num">{_rupees(row.value)}</td><td colspan="2">{MISSING}</td>'
        )
    else:
        cmp = MISSING
        if row.cmp is not None and row.cmp_session is not None:
            cmp = f'{_rupees(row.cmp)} <span class="muted">{row.cmp_session.isoformat()}</span>'
        note = (
            "" if row.note is None else f'<div class="muted">{free_text(row.note, limit=240)}</div>'
        )
        cmp_cells = (
            f'<td class="num">{cmp}{note}</td>'
            + f'<td class="num">{_rupees(row.average_price)}</td>'
            + _change_cells(row)
            + f'<td class="num">{_rupees(row.value)}</td>'
            + f'<td class="num"{_tone(row.profit)}>{_rupees(row.profit, signed=True)}</td>'
            + f'<td class="num"{_tone(row.profit)}>{_percent(row.profit_pct)}</td>'
        )
    quantity = MISSING if row.quantity is None else str(row.quantity)
    return (
        f'<tr {attrs}>{name}<td class="num">{quantity}</td>{cmp_cells}<td>{action_cell}</td></tr>'
    )


def _holdings(page: PortfolioOut) -> str:
    """Only what the book holds: every row has a quantity. A staged BUY of a name not held is in
    `_buying`, never here."""
    if page.bench_note is not None:
        return (
            f'<div class="panel" id="holdings"><h2>Holdings</h2><p>{esc(page.bench_note)}</p></div>'
        )
    if page.session is None:
        return ""
    held = [row for row in page.rows if row.held]
    session = page.session.isoformat()
    if not held:
        first = (
            f" — first orders fill on {page.next_session.isoformat()}"
            if page.next_session is not None
            and any(r.action is StagedAction.BUY for r in page.rows if not r.held)
            else ""
        )
        return (
            f'<div class="panel" id="holdings"><h2>Holdings <span class="muted">as marked on '
            f'{session}</span></h2><p id="no-holdings">No holdings yet{first}.</p></div>'
        )
    next_session = MISSING if page.next_session is None else page.next_session.isoformat()
    return (
        '<div class="panel" id="holdings">'
        f'<h2>Holdings <span class="muted">as marked on {session} · next session '
        f"{next_session}</span></h2>"
        "<table><thead><tr><th>Symbol</th><th>ISIN</th><th>Qty</th><th>CMP</th>"
        "<th>Avg price</th><th>Day's change / share</th><th>Day's change (position)</th>"
        "<th>Day's change %</th><th>Current value</th>"
        "<th>Profit</th><th>Profit %</th><th>Next session</th></tr></thead><tbody>"
        + "".join(_row(row) for row in held)
        + "</tbody></table>"
        '<p class="muted">CMP is the close the book was marked at; the average price is the cost '
        "basis per share, buy charges included; the day's change is against the previous session's "
        "close, per share and for the whole position (per share x quantity held), and its % is "
        "the same for both; profit is on cost. Next session shows only an order staged for it: "
        "SELL n for a sale or a trim, BUY n for an add.</p></div>"
    )


def _buying_row(row: HoldingRowOut) -> str:
    attrs = (
        f'data-buying="{esc(row.isin)}" data-quantity="{row.action_quantity or ""}" '
        f'data-cmp="{paisa(row.cmp)}" data-day-change-pct="{pct(row.day_change_pct)}" '
        f'data-estimated-cost="{paisa(row.estimated_cost)}"'
    )
    cmp = MISSING
    if row.cmp is not None and row.cmp_session is not None:
        cmp = f'{_rupees(row.cmp)} <span class="muted">{row.cmp_session.isoformat()}</span>'
    note = "" if row.note is None else f'<div class="muted">{free_text(row.note, limit=240)}</div>'
    symbol = esc(row.symbol) if row.symbol else MISSING
    return (
        f'<tr {attrs}><td>{symbol}</td><td class="mono">{esc(row.isin)}</td>'
        f'<td class="num">{row.action_quantity}</td><td class="num">{cmp}{note}</td>'
        f'<td class="num"{_tone(row.day_change_pct)}>{_percent(row.day_change_pct)}</td>'
        f'<td class="num">{_rupees(row.estimated_cost)}</td></tr>'
    )


def _buying(page: PortfolioOut) -> str:
    """Staged BUYs of names the book does not hold yet; omitted when there are none."""
    buys = [r for r in page.rows if not r.held and r.action is StagedAction.BUY]
    if not buys:
        return ""
    when = MISSING if page.next_session is None else page.next_session.isoformat()
    return (
        f'<div class="panel" id="buying"><h2>Buying next session ({when})</h2>'
        "<table><thead><tr><th>Symbol</th><th>ISIN</th><th>Qty to buy</th><th>CMP</th>"
        "<th>Day's change %</th><th>Est. cost</th></tr></thead><tbody>"
        + "".join(_buying_row(row) for row in buys)
        + "</tbody></table>"
        '<p class="muted">Names the book does not hold yet, with a BUY staged for the next '
        "session. Est. cost is quantity x CMP — an estimate before charges; the order fills at "
        "the next session's price.</p></div>"
    )


# ── the totals ─────────────────────────────────────────────────────────────────────────────


def _totals(totals: FundTotalsOut) -> str:
    attrs = (
        f'data-session="{totals.session.isoformat()}" data-invested="{paisa(totals.invested)}" '
        f'data-current-value="{paisa(totals.current_value)}" data-cash="{paisa(totals.cash)}" '
        f'data-nav="{paisa(totals.nav)}" data-total-profit="{paisa(totals.total_profit)}" '
        f'data-total-profit-pct="{pct(totals.total_profit_pct)}" '
        f'data-daily-change="{paisa(totals.daily_change)}" '
        f'data-daily-change-pct="{pct(totals.daily_change_pct)}" '
        f'data-reconciled="{str(totals.reconciled).lower()}"'
    )
    previous = (
        "first marked session"
        if totals.previous_session is None
        else f"vs {totals.previous_session.isoformat()} NAV {_rupees(totals.previous_nav)}"
    )
    lines = (
        ("Invested (cost of holdings)", _rupees(totals.invested), ""),
        ("Current value of holdings", _rupees(totals.current_value), ""),
        ("Funds balance (cash)", _rupees(totals.cash), ""),
        (
            "Total (NAV)",
            f"<strong>{_rupees(totals.nav)}</strong>",
            f"{totals.positions} position(s)",
        ),
        (
            "Total profit",
            f"<span{_tone(totals.total_profit)}>{_rupees(totals.total_profit, signed=True)} "
            f"({_percent(totals.total_profit_pct)})</span>",
            f"vs opening capital {_rupees(totals.opening_capital)}",
        ),
        (
            "Daily change",
            f"<span{_tone(totals.daily_change)}>{_rupees(totals.daily_change, signed=True)} "
            f"({_percent(totals.daily_change_pct)})</span>",
            previous,
        ),
    )
    rows = "".join(
        f'<tr><th>{esc(label)}</th><td class="num">{value}</td><td class="muted">{note}</td></tr>'
        for label, value, note in lines
    )
    return (
        f'<div class="panel"><h2>Fund <span class="muted">marked {totals.session.isoformat()}'
        f'</span></h2><table id="totals" {attrs} style="width:auto"><tbody>{rows}</tbody></table>'
        '<p class="muted">NAV and cash are the book\'s own mark (the figures the digest and '
        "/status/managers report).</p></div>"
    )


def render_portfolio(page: PortfolioOut, *, fragment: bool = False) -> str:
    """The Portfolio page for one book: book picker, stream label, holdings and fund totals.

    With ``fragment`` it is only the portfolio region (picker included) for an htmx swap — no
    document around it; its ``<title>`` is the one htmx sets on the tab.
    """
    book = page.book
    label = (
        f'<p id="book-label" data-book="{esc(book.book_id)}" data-kind="{esc(book.kind)}">'
        f'<span class="state">{esc(book.kind)}</span> <span class="muted">{_owner(book)} · '
        f"opening capital {_rupees(book.opening_capital_inr)}</span></p>"
    )
    parts = [
        f'<h1>Portfolio · <span class="mono">{esc(book.book_id)}</span></h1>',
        _picker(page),
        _stream_banner(page),
        label,
    ]
    if page.error is not None:
        parts.append(
            '<div class="banner bad" id="portfolio-error">The journal could not be read: '
            f"<pre>{free_text(page.error, limit=1000)}</pre></div>"
        )
    if page.missing:
        parts.append(
            '<div class="banner warn" id="missing">'
            + "".join(f"<div>{free_text(text, limit=400)}</div>" for text in page.missing)
            + "</div>"
        )
    if page.totals is not None:
        parts.append(_totals(page.totals))
    parts.append(_holdings(page))
    parts.append(_buying(page))
    title = f"Portfolio {book.book_id}"
    region = "".join(parts)
    if fragment:
        head = f"<title>{esc(title)} · trading-platform console</title>"
        return f'<div id="{REGION_ID}">{head}{region}</div>'
    return _page(title, _SLUG, f'<div id="{REGION_ID}">{region}</div>', as_of=page.as_of)
