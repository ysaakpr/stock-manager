"""The Desk page (M18.3) as a pure function of the wire models: models in, HTML out.

Same rules as `console.pages`: no I/O, no clock, no route logic; every free-text field through
`render.free_text`, every instant through `render.ist`. The paper and managers panels render
`PaperOut` and `ManagersOut` exactly as `/status/paper` and `/status/managers` serve them, and
each row carries its key fields as ``data-*`` attributes so a test can read the page back and
compare it with the JSON.

The kill switch and the broker session are **display only**: there is no form, no button and no
link that changes either, here or anywhere on the console (read-only until M18.5, and M18.5 does
not touch them). An engaged switch, an unreadable one, or a broker session the journal says needs
re-authentication renders as a red banner at the top of the page, above everything else.

Money is formatted from the `Decimal` it arrived as (`_amount`), never through a float.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

from dataplatform.status.console.pages import _page
from dataplatform.status.console.render import MISSING, esc, free_text, ist
from dataplatform.status.models import (
    ManagerDecisionOut,
    ManagersOut,
    PaperBookStatusOut,
    PaperOut,
)
from dataplatform.status.models_desk import (
    BrokerSessionOut,
    BrokerSessionState,
    MandateOut,
    ModelCostOut,
    OrderLineOut,
    OrdersOut,
    PaperPositionsOut,
)

__all__ = ["render_desk", "switch_state"]

_CELL_TEXT: Final = 240

#: The console's existing state colours (base.html), by tone — the Desk adds no CSS of its own.
_TONE: Final = {"bad": "FAILED", "warn": "warning", "info": "info", "ok": "OK"}

#: Order states that are refusals or failures, and those still in flight.
_REFUSED: Final = frozenset({"RAIL_BLOCKED", "REJECTED", "CANCELLED"})
_IN_FLIGHT: Final = frozenset({"STAGED", "SENT", "PARTIAL", "PLACED"})


def _flag(text: object, tone: str) -> str:
    return f'<span class="state state-{_TONE[tone]}">{esc(text)}</span>'


def _amount(value: Decimal | None) -> str:
    """A Decimal as text, grouped, at the precision it was recorded with; ``—`` when absent."""
    if value is None:
        return MISSING
    return f"{value:,}"


def _cell(value: object) -> str:
    return MISSING if value is None else esc(value)


def _table(head: Sequence[str], rows: str, *, empty: str) -> str:
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    cells = "".join(f"<th>{esc(h)}</th>" for h in head)
    return f"<table><thead><tr>{cells}</tr></thead><tbody>{rows}</tbody></table>"


# ── the banners ─────────────────────────────────────────────────────────────────────────────


def switch_state(book: PaperBookStatusOut) -> str:
    """ENGAGED (tripped), ARMED, or UNREADABLE (the state file could not be read — never armed)."""
    tripped = book.kill_switch.tripped
    if tripped is None:
        return "UNREADABLE"
    return "ENGAGED" if tripped else "ARMED"


def _kill_switch_banner(paper: PaperOut) -> str:
    states = {book.book_id: switch_state(book) for book in paper.books}
    red = [book for book in paper.books if states[book.book_id] != "ARMED"]
    if red:
        engaged = any(states[book.book_id] == "ENGAGED" for book in red)
        items = "".join(
            f'<li data-switch-book="{esc(book.book_id)}"><span class="mono">'
            f"{esc(book.book_id)}</span> {_flag(states[book.book_id], 'bad')} · source "
            f"{_cell(book.kill_switch.source)} · tripped {ist(book.kill_switch.tripped_at)} · "
            f"{free_text(book.kill_switch.reason or book.kill_switch.error, limit=400)}</li>"
            for book in red
        )
        title = "KILL SWITCH ENGAGED" if engaged else "KILL SWITCH STATE UNREADABLE"
        return (
            f'<div class="banner bad" id="kill-switch" data-state="'
            f'{"ENGAGED" if engaged else "UNREADABLE"}"><strong>{title}</strong> — '
            f"{len(red)} of {len(paper.books)} paper books will place no order"
            f"<ul>{items}</ul>"
            '<div class="muted">display only: this console cannot trip or reset a switch</div>'
            "</div>"
        )
    if not paper.books:
        return (
            '<div class="banner warn" id="kill-switch" data-state="NONE">Kill switch: no paper '
            "book and no kill-switch file is known</div>"
        )
    return (
        '<div class="banner ok" id="kill-switch" data-state="ARMED">Kill switch armed on all '
        f"{len(paper.books)} paper books · display only</div>"
    )


def _broker_banner(broker: BrokerSessionOut) -> str:
    state = broker.state
    seen = f"newest journaled session {_cell(broker.newest_journaled_date)}"
    if state is BrokerSessionState.AUTH_REQUIRED:
        deferred = MISSING if broker.deferred_count is None else str(broker.deferred_count)
        return (
            '<div class="banner bad" id="broker-session" data-state="AUTH_REQUIRED">'
            "<strong>BROKER AUTH REQUIRED</strong> — the broker session is not authenticated; "
            "the daily loop places no order and defers each staged decision"
            f"<div>journaled for {_cell(broker.last_auth_required_date)} at "
            f"{ist(broker.last_auth_required_at)} · streak since {_cell(broker.streak_started)} "
            f"· {deferred} decisions deferred · {seen}</div>"
            f"<div>reason: {free_text(broker.reason, limit=400)}</div>"
            f"<div>{free_text(broker.reauth, limit=600)}</div>"
            '<div class="muted">display only: re-authentication happens in the broker login flow, '
            "never on this page</div></div>"
        )
    if state is BrokerSessionState.CLEAR:
        detail = (
            f"no AUTH_REQUIRED on the {seen}; the last was journaled for "
            f"{_cell(broker.last_auth_required_date)}"
        )
    else:
        detail = f"no AUTH_REQUIRED has ever been journaled · {seen}"
    return (
        f'<div class="banner ok" id="broker-session" data-state="{esc(state.value)}">'
        f"Broker session {_flag(state.value, 'ok')} · {detail} · read from the decision journal, "
        "display only</div>"
    )


# ── paper books ─────────────────────────────────────────────────────────────────────────────


def _paper_row(book: PaperBookStatusOut) -> str:
    state = switch_state(book)
    blocks = (
        "".join(f'<div class="mono">{free_text(item, limit=200)}</div>' for item in book.unresolved)
        or MISSING
    )
    recon = book.latest_recon
    recon_cell = MISSING if recon is None else _flag(recon, "ok" if recon == "CLEAN" else "bad")
    outcome = book.latest_outcome
    outcome_cell = (
        MISSING if outcome is None else _flag(outcome, "ok" if outcome == "COMPLETED" else "bad")
    )
    healthy = "true" if book.healthy else "false"
    return (
        f'<tr data-paper-book="{esc(book.book_id)}" data-healthy="{healthy}" '
        f'data-switch="{state}" data-outcome="{_cell(outcome)}" data-recon="{_cell(recon)}" '
        f'data-unresolved="{len(book.unresolved)}">'
        f'<td class="mono">{esc(book.book_id)}</td>'
        f"<td>{_flag('healthy', 'ok') if book.healthy else _flag('blocked', 'bad')}</td>"
        f"<td>{_flag(state, 'ok' if state == 'ARMED' else 'bad')}</td>"
        f"<td>{_cell(book.latest_date)}</td><td>{outcome_cell}</td>"
        f"<td><pre>{free_text(book.latest_reason, limit=_CELL_TEXT)}</pre></td>"
        f"<td>{recon_cell}</td><td>{blocks}</td></tr>"
    )


def _positions(positions: Sequence[PaperPositionsOut]) -> str:
    parts = []
    for book in positions:
        rows = "".join(
            f'<tr data-position="{esc(book.book_id)}:{esc(p.isin)}"><td class="mono">'
            f'{esc(p.isin)}</td><td class="num">{p.quantity:,}</td>'
            f'<td class="num">{_amount(p.cost_basis)}</td></tr>'
            for p in book.positions
        )
        parts.append(
            f'<h3 class="mono">{esc(book.book_id)} <span class="muted">after '
            f"{esc(book.trading_date)} · cash ₹{_amount(book.cash)} · {len(book.positions)} "
            "positions</span></h3>"
            + _table(("ISIN", "Quantity", "Cost basis ₹"), rows, empty="flat: no position")
        )
    return "".join(parts) or (
        '<p class="muted">no paper session has recorded an accounting book yet</p>'
    )


def _paper_panel(paper: PaperOut, positions: Sequence[PaperPositionsOut]) -> str:
    rows = "".join(_paper_row(book) for book in paper.books)
    verdict = _flag("healthy", "ok") if paper.healthy else _flag("not healthy", "bad")
    return (
        f'<div class="panel" id="paper" data-healthy="{"true" if paper.healthy else "false"}">'
        f'<h2>Paper books <span class="muted">{len(paper.books)} · same read as '
        '<span class="mono">/status/paper</span></span></h2>'
        f"<p>{verdict} · a book is blocked by an engaged switch, a reconciliation break, or an "
        "escalation with no resolution row (Blocks)</p>"
        + _table(
            (
                "Book",
                "Next session",
                "Kill switch",
                "Latest session",
                "Outcome",
                "Reason",
                "Recon",
                "Blocks",
            ),
            rows,
            empty="no paper book",
        )
        + "<h2>Positions</h2>"
        + _positions(positions)
        + "</div>"
    )


# ── the M17 desk ────────────────────────────────────────────────────────────────────────────


def _mandates(mandates: Sequence[MandateOut]) -> str:
    rows = "".join(
        f'<tr data-mandate="{esc(m.book_id)}"><td class="mono">{esc(m.book_id)}</td>'
        f'<td>{esc(m.kind)}</td><td class="mono">{_cell(m.manager)}</td><td>{_cell(m.role)}</td>'
        f'<td>{_cell(m.style)}</td><td class="num">{_amount(m.opening_capital_inr)}</td>'
        f'<td class="num">{_cell(m.max_positions)}</td>'
        f'<td class="num">{_amount(m.max_position_pct)}</td>'
        f'<td class="num">{_amount(m.max_sector_pct)}</td>'
        f'<td class="mono">{_cell(m.decision_model)}</td></tr>'
        for m in mandates
    )
    return "<h3>Mandates</h3>" + _table(
        (
            "Book",
            "Kind",
            "Manager",
            "Role",
            "Style",
            "Capital ₹",
            "Max names",
            "Max name %",
            "Max sector %",
            "Decision model",
        ),
        rows,
        empty="the roster lists no book",
    )


def _books(managers: ManagersOut) -> str:
    rows = "".join(
        f'<tr data-m17-book="{esc(b.book_id)}" data-nav="{_cell(b.nav)}">'
        f'<td class="mono">{esc(b.book_id)}</td><td>{esc(b.kind)}</td>'
        f'<td class="num">{_amount(b.opening_capital_inr)}</td><td>{_cell(b.latest_session)}</td>'
        f'<td class="num">{_amount(b.nav)}</td><td class="num">{_amount(b.cash)}</td>'
        f'<td class="num">{_cell(b.positions)}</td><td class="num">{_amount(b.return_pct)}</td>'
        "</tr>"
        for b in managers.books
    )
    return "<h3>Books</h3>" + _table(
        ("Book", "Kind", "Opening ₹", "Latest mark", "NAV ₹", "Cash ₹", "Positions", "Return %"),
        rows,
        empty="no M17 book has journaled a mark",
    )


_VERDICT_TONE: Final = {"PASS": "ok", "CLEAR_FAIL": "bad", "INCONCLUSIVE": "warn"}


def _scoreboard(managers: ManagersOut) -> str:
    if managers.scoreboard_error is not None:
        head = (
            '<div class="banner bad" id="scoreboard-error"><strong>Scoreboard could not be built'
            f"</strong>: {free_text(managers.scoreboard_error, limit=600)}</div>"
        )
    else:
        head = (
            f'<p id="scoreboard" data-k-of-n="{esc(managers.k_of_n)}">'
            f"<strong>{esc(managers.k_of_n)}</strong> · scoreboard as of "
            f"{_cell(managers.scoreboard_as_of)} · digest "
            f'<span class="mono">{_cell((managers.scoreboard_digest or "")[:16] or None)}</span>'
            "</p>"
        )
    score_rows = "".join(
        f'<tr data-score="{esc(s.book_id)}" data-verdict="{esc(s.verdict)}">'
        f'<td class="mono">{esc(s.book_id)}</td><td>{esc(s.role)}</td>'
        f'<td class="mono">{esc(s.control_id)}</td><td>{esc(s.phase)}</td>'
        f"<td>{_flag(s.verdict, _VERDICT_TONE.get(s.verdict, 'info'))}</td>"
        f"<td>{_cell(s.window)} {_cell(s.window_sessions)}</td>"
        f'<td class="num">{_amount(s.excess_vs_control_pp)}</td>'
        f'<td class="num">{_amount(s.excess_vs_bench_pp)}</td>'
        f'<td class="num">{_amount(s.max_drawdown_pp)}</td>'
        f'<td class="num">{_amount(s.brier)}</td>'
        f'<td class="num">{_cell(s.resolved_decisions)}</td></tr>'
        for s in managers.managers
    )
    result_rows = "".join(
        f'<tr data-manager-result="{esc(r.manager_id)}" data-passed-on="{esc(r.passed_on)}">'
        f'<td class="mono">{esc(r.manager_id)}</td><td>{esc(r.primary_verdict)}</td>'
        f"<td>{esc(r.mirror_verdict)}</td><td>{esc(r.passed_on)}</td>"
        f'<td class="num">{r.decisions}</td><td class="num">{_amount(r.brier)}</td>'
        f"<td>{'yes' if r.extension_confirmed else 'no'}</td></tr>"
        for r in managers.manager_results
    )
    style_rows = "".join(
        f'<tr data-style-book="{esc(b.style_id)}"><td class="mono">{esc(b.style_id)}</td>'
        f'<td class="mono">{esc(b.primary_book)}</td><td>{_cell(b.window)}</td>'
        f'<td class="num">{_amount(b.style_return_pct)}</td>'
        f'<td class="num">{_amount(b.primary_return_pct)}</td>'
        f'<td class="num">{_amount(b.primary_excess_vs_style_pp)}</td></tr>'
        for b in managers.style_books
    )
    graduation = managers.graduation
    floor = (
        ""
        if graduation is None
        else f'<p id="graduation">Graduation floor: {free_text(graduation.rule, limit=400)} · '
        f"{graduation.primary_passes} of {graduation.primary_passes_needed} primary passes · "
        f"{'met' if graduation.met else 'not met'}</p>"
    )
    return (
        "<h3>Scoreboard</h3>"
        + head
        + _table(
            (
                "Book",
                "Role",
                "Control",
                "Phase",
                "Verdict",
                "Window",
                "vs control pp",
                "vs bench pp",
                "Max DD pp",
                "Brier",
                "Resolved",
            ),
            score_rows,
            empty="no manager book is scored yet",
        )
        + _table(
            ("Manager", "Primary", "Mirror", "Passed on", "Decisions", "Brier", "Extension"),
            result_rows,
            empty="no manager result yet",
        )
        + _table(
            (
                "Style book",
                "Primary",
                "Window",
                "Style return %",
                "Primary return %",
                "Primary vs style pp",
            ),
            style_rows,
            empty="no style book scored yet",
        )
        + floor
    )


def _is_refusal(line: ManagerDecisionOut) -> bool:
    return line.decision == "RAIL_BLOCK" or line.refused_for is not None


def _decisions(managers: ManagersOut) -> str:
    rows = "".join(
        f'<tr data-decision="{esc(d.book_id)}" data-kind="{esc(d.decision)}" '
        f'data-event="{_cell(d.event)}" data-refused="{"true" if _is_refusal(d) else "false"}">'
        f'<td class="mono">{esc(d.book_id)}</td>'
        f"<td>{_flag(d.decision, 'bad' if _is_refusal(d) else 'info')}</td>"
        f'<td>{_cell(d.event)}</td><td class="mono">{_cell(d.isin)}</td>'
        f'<td>{_cell(d.action)}</td><td class="num">{_cell(d.target_weight)}</td>'
        f'<td class="num">{_cell(d.p_beat_bench)}</td>'
        f'<td class="num">{_cell(d.horizon_sessions)}</td>'
        f"<td>{free_text(d.rails, limit=200)}</td><td>{free_text(d.refused_for, limit=200)}</td>"
        "</tr>"
        for d in managers.decisions
    )
    refused = sum(1 for d in managers.decisions if _is_refusal(d))
    suspended_rows = "".join(
        f'<tr data-suspended="{esc(s.book_id)}:{esc(s.isin)}"><td class="mono">'
        f'{esc(s.book_id)}</td><td class="mono">{esc(s.isin)}</td>'
        f"<td>held, not trading since {esc(s.last_trade_date)}</td>"
        f'<td class="num">{s.sessions_suspended}</td></tr>'
        for s in managers.suspended_holdings
    )
    return (
        f'<h3>Decisions <span class="muted">session {_cell(managers.decisions_session)} · '
        f"{len(managers.decisions)} lines, {refused} refused</span></h3>"
        + _table(
            (
                "Book",
                "Decision",
                "Event",
                "ISIN",
                "Action",
                "Target weight",
                "P(beat bench)",
                "Horizon",
                "Rails",
                "Refused for",
            ),
            rows,
            empty="no decision journaled yet",
        )
        + f'<h3>Suspended holdings <span class="muted">{len(managers.suspended_holdings)}'
        "</span></h3>"
        + _table(
            ("Book", "ISIN", "Status", "Sessions suspended"),
            suspended_rows,
            empty="no held name is suspended on the latest session",
        )
    )


def _managers_panel(managers: ManagersOut, mandates: Sequence[MandateOut]) -> str:
    return (
        '<div class="panel" id="managers"><h2>M17 fund managers <span class="muted">same read '
        'as <span class="mono">/status/managers</span></span></h2>'
        f"<p>S0 {_cell(managers.s0)} · {managers.sessions_elapsed} sessions elapsed</p>"
        + _mandates(mandates)
        + _books(managers)
        + _decisions(managers)
        + _scoreboard(managers)
        + "</div>"
    )


# ── orders ──────────────────────────────────────────────────────────────────────────────────


def _order_tone(state: str) -> str:
    if state in _REFUSED:
        return "bad"
    return "info" if state in _IN_FLIGHT else "ok"


def _order_row(line: OrderLineOut) -> str:
    tags = " ".join(f'<span class="mono">{esc(tag)}</span>' for tag in line.tags) or MISSING
    return (
        f'<tr data-order="{esc(line.source)}:{esc(line.book)}:{esc(line.isin)}" '
        f'data-state="{esc(line.state)}"><td class="mono">{esc(line.book)}</td>'
        f'<td class="mono">{_cell(line.reference)}</td><td>{esc(line.trading_date)}</td>'
        f'<td>{ist(line.staged_at)}</td><td class="mono">{esc(line.isin)}</td>'
        f'<td>{esc(line.side)}</td><td class="num">{line.quantity:,}</td>'
        f"<td>{esc(line.order_type)} {_amount(line.limit_price) if line.limit_price else ''}</td>"
        f"<td>{_flag(line.state, _order_tone(line.state))}</td><td>{tags}</td>"
        f'<td class="num">{_cell(line.filled_quantity)}</td>'
        f'<td class="num">{_amount(line.avg_fill_price_inr)}</td>'
        f'<td class="num">{_amount(line.costs_inr)}</td><td>{ist(line.executed_at)}</td>'
        f"<td>{free_text(line.rejection_reason, limit=_CELL_TEXT)}</td></tr>"
    )


_ORDER_HEAD: Final = (
    "Book / case",
    "Order uid",
    "For session",
    "Staged",
    "ISIN",
    "Side",
    "Qty",
    "Type",
    "State",
    "Tags",
    "Filled",
    "Avg fill ₹",
    "Costs ₹",
    "Executed",
    "Refusal",
)


def _orders_panel(orders: OrdersOut) -> str:
    ledger = "".join(_order_row(line) for line in orders.ledger)
    paper = "".join(_order_row(line) for line in orders.paper)
    return (
        '<div class="panel" id="orders"><h2>Orders</h2>'
        f'<h3>Order ledger <span class="muted">order_, newest {orders.limit} at most · '
        f"{len(orders.ledger)} shown</span></h3>"
        + _table(_ORDER_HEAD, ledger, empty="no order_ row: nothing has been staged on this path")
        + '<h3>Paper sessions <span class="muted">each paper book\'s newest session that placed '
        "orders on the paper broker</span></h3>"
        + _table(_ORDER_HEAD, paper, empty="no paper session has placed an order")
        + '<p class="muted">M17 books\' staged and refused orders are their BUY, SELL and '
        "RAIL_BLOCK decisions above.</p></div>"
    )


# ── model cost ──────────────────────────────────────────────────────────────────────────────


def _cost_panel(cost: ModelCostOut) -> str:
    rows = "".join(
        f'<tr data-cost="{_cell(line.case_id)}:{esc(line.model)}:{esc(line.day)}" '
        f'data-inr="{line.cost_inr}" data-usd="{_cell(line.cost_usd)}">'
        f'<td>{esc(line.day)}</td><td class="mono">{_cell(line.case_id)}</td>'
        f'<td>{esc(line.provider)}</td><td class="mono">{esc(line.model)}</td>'
        f'<td class="num">{line.calls}</td><td class="num">{line.tokens_in:,}</td>'
        f'<td class="num">{line.tokens_out:,}</td><td class="num">{_amount(line.cost_inr)}</td>'
        f'<td class="num">{_amount(line.cost_usd)}'
        f"{'' if line.usd_calls in (0, line.calls) else f' ({line.usd_calls} of {line.calls})'}"
        "</td></tr>"
        for line in cost.lines
    )
    return (
        f'<div class="panel" id="model-cost" data-total-inr="{cost.total_inr}" '
        f'data-total-usd="{_cell(cost.total_usd)}"><h2>Model cost <span class="muted">'
        f"token_usage, last {cost.days} days from {ist(cost.since)}</span></h2>"
        f"<p>Total ₹{_amount(cost.total_inr)} · USD {_amount(cost.total_usd)} as billed "
        "(USD only where the call recorded it)</p>"
        + _table(
            (
                "Day (IST)",
                "Manager / case",
                "Provider",
                "Model",
                "Calls",
                "Tokens in",
                "Tokens out",
                "Cost ₹",
                "Cost USD",
            ),
            rows,
            empty=f"no model call recorded in the last {cost.days} days",
        )
        + "</div>"
    )


# ── the page ────────────────────────────────────────────────────────────────────────────────


def render_desk(
    *,
    paper: PaperOut,
    positions: Sequence[PaperPositionsOut],
    managers: ManagersOut,
    mandates: Sequence[MandateOut],
    broker: BrokerSessionOut,
    orders: OrdersOut,
    cost: ModelCostOut,
    as_of: datetime,
) -> str:
    """The Desk: kill-switch and broker banners, paper books, M17 managers, orders, model cost."""
    body = (
        "<h1>Desk</h1>"
        + _kill_switch_banner(paper)
        + _broker_banner(broker)
        + _paper_panel(paper, positions)
        + _managers_panel(managers, mandates)
        + _orders_panel(orders)
        + _cost_panel(cost)
    )
    return _page("Desk", "desk", body, as_of=as_of)
