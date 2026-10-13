"""The Portfolio page (M18.7) as a pure function of `PortfolioOut`: model in, HTML out.

Money is printed to the paisa from the model's exact `Decimal` (half-up, the way a statement
rounds), with Indian digit grouping; a percentage to two places. An absent figure is `MISSING`,
never a zero. The same rounded values go into each row's ``data-*`` attributes, so a test reads
what the operator reads.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from dataplatform.status.console.pages import _page
from dataplatform.status.console.render import MISSING, esc, free_text
from dataplatform.status.models_portfolio import (
    BookChoiceOut,
    FundTotalsOut,
    HoldingRowOut,
    PortfolioOut,
    PortfolioStream,
)

__all__ = ["paisa", "pct", "render_portfolio"]

_SLUG: Final = "portfolio"
_PAISA: Final = Decimal("0.01")
#: The selector's groups, in this order; a kind the roster adds later gets its own group after.
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


# ── the selector and the labels ────────────────────────────────────────────────────────────


def _owner(book: BookChoiceOut) -> str:
    if book.kind == "BENCH" or book.manager is None:
        return esc(book.kind.lower())
    relation = {"MANAGER": "manager", "CONTROL": "control for", "STYLE": "style book of"}
    return f"{esc(relation.get(book.kind, book.kind.lower()))} {esc(book.manager)}"


def _selector(page: PortfolioOut) -> str:
    kinds = list(_KIND_LABELS) + sorted({b.kind for b in page.books} - set(_KIND_LABELS))
    groups = []
    for kind in kinds:
        options = "".join(
            f'<option value="{esc(b.book_id)}"'
            f"{' selected' if b.book_id == page.book.book_id else ''}>{esc(b.book_id)}</option>"
            for b in page.books
            if b.kind == kind
        )
        if options:
            groups.append(
                f'<optgroup label="{esc(_KIND_LABELS.get(kind, kind))}">{options}</optgroup>'
            )
    return (
        '<form method="get" action="/console/portfolio" id="book-select" class="panel">'
        '<label>Book <select name="book">' + "".join(groups) + "</select></label>"
        ' <button type="submit">Show</button></form>'
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


def _change_cells(row: HoldingRowOut) -> str:
    return (
        f'<td class="num"{_tone(row.day_change)}>{_rupees(row.day_change, signed=True)}</td>'
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
            f'<td colspan="2" class="muted">{free_text(row.note, limit=240)}</td>'
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
    if page.bench_note is not None:
        return (
            f'<div class="panel" id="holdings"><h2>Holdings</h2><p>{esc(page.bench_note)}</p></div>'
        )
    if not page.rows:
        return ""
    next_session = MISSING if page.next_session is None else page.next_session.isoformat()
    session = MISSING if page.session is None else page.session.isoformat()
    return (
        '<div class="panel" id="holdings">'
        f'<h2>Holdings <span class="muted">as marked on {session} · next session '
        f"{next_session}</span></h2>"
        "<table><thead><tr><th>Symbol</th><th>ISIN</th><th>Qty</th><th>CMP</th>"
        "<th>Avg price</th><th>Day's change</th><th>Day's change %</th><th>Current value</th>"
        "<th>Profit</th><th>Profit %</th><th>Next session</th></tr></thead><tbody>"
        + "".join(_row(row) for row in page.rows)
        + "</tbody></table>"
        '<p class="muted">CMP is the close the book was marked at; the average price is the cost '
        "basis per share, buy charges included; profit is on cost. Next session shows only an "
        "order staged for it (a trim shows as SELL with its quantity).</p></div>"
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


def render_portfolio(page: PortfolioOut) -> str:
    """The Portfolio page for one book: selector, stream label, holdings and fund totals."""
    book = page.book
    label = (
        f'<p id="book-label" data-book="{esc(book.book_id)}" data-kind="{esc(book.kind)}">'
        f'<span class="state">{esc(book.kind)}</span> <span class="muted">{_owner(book)} · '
        f"opening capital {_rupees(book.opening_capital_inr)}</span></p>"
    )
    parts = [
        f'<h1>Portfolio · <span class="mono">{esc(book.book_id)}</span></h1>',
        _selector(page),
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
    return _page(f"Portfolio {book.book_id}", _SLUG, "".join(parts), as_of=page.as_of)
