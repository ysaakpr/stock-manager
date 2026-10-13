"""D5: the reads behind the console's Desk page (M18.3) that `status.queries` does not already own.

The paper books and the M17 desk are read by `status.queries.read_paper_status` and
`read_managers_status` — the functions `/status/paper` and `/status/managers` call — and the Desk
renders those results unchanged, so the page cannot disagree with either endpoint. This file is
the remainder: each paper book's positions, the broker-session interlock's journaled state, the
latest orders, the M17 mandates and the model spend in `token_usage`.

Same contract as `status.queries`: every function takes an open connection (or the roster the
managers read already resolved) and returns wire models; none opens a connection, reads the clock,
imports `execution`/`analyst`, or writes. The instant a window is relative to is passed in (B10).

**Money never passes through a float.** psycopg hands `numeric` back as `Decimal` and jsonb money
was written as strings (`paper_session`'s own rule); `_money` accepts exactly those two and refuses
anything else — a float reaching this layer is a defect upstream, and rounding it into a Decimal
here would hide it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from dataplatform.clock import IST
from dataplatform.redaction import mask_and_truncate
from dataplatform.status.models_desk import (
    BrokerSessionOut,
    BrokerSessionState,
    MandateOut,
    ModelCostLineOut,
    ModelCostOut,
    OrderLineOut,
    OrdersOut,
    PaperPositionOut,
    PaperPositionsOut,
)
from dataplatform.status.queries import StatusQueryError
from dataplatform.store.db import Connection

__all__ = [
    "MODEL_COST_DAYS",
    "ORDERS_LIMIT",
    "model_cost_since",
    "read_broker_session",
    "read_mandates",
    "read_model_cost",
    "read_orders",
    "read_paper_positions",
]

#: How many newest `order_` rows the Desk lists.
ORDERS_LIMIT: Final = 50

#: The model-cost window, in IST days ending on (and including) the page's own day.
MODEL_COST_DAYS: Final = 30

#: Free text read here (a gate's reason, a rejection) is masked before it leaves the query layer.
_TEXT_CHARS: Final = 2000


def _money(value: object, what: str) -> Decimal | None:
    """`value` as an exact `Decimal`: a `Decimal` as is, a decimal string parsed; nothing else.

    What it never does: accept a float (or a bool, or an int standing in for a JSON number) — a
    binary float is already a rounded number, and silently converting it would hide where it came
    from.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        try:
            number = Decimal(value)
        except InvalidOperation as error:
            raise StatusQueryError(f"{what}: {value!r} is not a decimal string") from error
    else:
        raise StatusQueryError(
            f"{what}: {value!r} is a {type(value).__name__}; money is read as Decimal or a "
            "decimal string, never a float"
        )
    if not number.is_finite():
        raise StatusQueryError(f"{what}: {value!r} is not a finite amount")
    return number


def _required_money(value: object, what: str) -> Decimal:
    number = _money(value, what)
    if number is None:
        raise StatusQueryError(f"{what}: missing")
    return number


def _whole(value: object, what: str) -> int:
    """A share count or a call count: an int, or a string of digits (jsonb); never a float."""
    if isinstance(value, bool):
        raise StatusQueryError(f"{what}: {value!r} is not a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    raise StatusQueryError(f"{what}: {value!r} is not a whole number")


def _masked(text: object) -> str | None:
    return None if text is None else mask_and_truncate(str(text), _TEXT_CHARS)


# ── paper positions ─────────────────────────────────────────────────────────────────────────

_PAPER_POSITIONS_SQL = """
SELECT DISTINCT ON (book_id) book_id, trading_date, expected_book
FROM paper_session
WHERE expected_book IS NOT NULL
ORDER BY book_id, trading_date DESC
"""


def read_paper_positions(conn: Connection) -> list[PaperPositionsOut]:
    """Each paper book's holdings, from its newest session that recorded an accounting book.

    The source is `paper_session.expected_book` (M15.3) — the paper book's own accounting side,
    the book the next session restores and reconciles the broker against. A book with no such row
    (one decided before M15.3, or not yet decided) is absent here, not shown as flat.
    """
    out: list[PaperPositionsOut] = []
    for book_id, trading_date, book in conn.execute(_PAPER_POSITIONS_SQL).fetchall():
        where = f"paper_session {book_id} {trading_date}"
        if not isinstance(book, Mapping):
            raise StatusQueryError(f"{where}: expected_book is {book!r}")
        positions = [
            PaperPositionOut(
                isin=str(item["isin"]),
                quantity=_whole(item.get("quantity"), f"{where} quantity"),
                cost_basis=_money(item.get("cost_basis"), f"{where} cost_basis"),
            )
            for item in book.get("positions") or []
        ]
        out.append(
            PaperPositionsOut(
                book_id=book_id,
                trading_date=trading_date,
                cash=_money(book.get("cash"), f"{where} cash"),
                positions=sorted(positions, key=lambda p: p.isin),
            )
        )
    return out


# ── the broker-session interlock ────────────────────────────────────────────────────────────

_NEWEST_JOURNALED_SQL = "SELECT max(trading_date) FROM decision_journal"

_LAST_AUTH_REQUIRED_SQL = """
SELECT trading_date, ts, rationale, payload->>'reauth', payload->>'streak_started',
       payload->>'deferred_count'
FROM decision_journal
WHERE decision = 'AUTH_REQUIRED'
ORDER BY trading_date DESC, id DESC
LIMIT 1
"""


def read_broker_session(conn: Connection) -> BrokerSessionOut:
    """The broker session as the decision journal records it (M5.15) — for display only.

    The auth interlock journals `AUTH_REQUIRED` on every trading day the broker session is dead and
    nothing on a clear day; every day journals something (invariant #9). So the session is
    ``AUTH_REQUIRED`` when the newest journaled trading date carries one, ``CLEAR`` when one exists
    only on an earlier date, and ``NO_LAPSE_RECORDED`` when there has never been one.
    What it never does: ask a broker — System 1 does not import `execution`, and this page is not a
    way to touch a session.
    """
    newest_row = conn.execute(_NEWEST_JOURNALED_SQL).fetchone()
    newest: date | None = None if newest_row is None else newest_row[0]
    last = conn.execute(_LAST_AUTH_REQUIRED_SQL).fetchone()
    if last is None:
        return BrokerSessionOut(
            state=BrokerSessionState.NO_LAPSE_RECORDED,
            newest_journaled_date=newest,
            last_auth_required_date=None,
            last_auth_required_at=None,
            reason=None,
            reauth=None,
            streak_started=None,
            deferred_count=None,
        )
    trading_date, ts, rationale, reauth, streak, deferred = last
    dead = newest is None or trading_date >= newest
    return BrokerSessionOut(
        state=BrokerSessionState.AUTH_REQUIRED if dead else BrokerSessionState.CLEAR,
        newest_journaled_date=newest,
        last_auth_required_date=trading_date,
        last_auth_required_at=ts,
        reason=_masked(rationale),
        reauth=_masked(reauth),
        streak_started=None if streak is None else date.fromisoformat(streak),
        deferred_count=None if deferred is None else _whole(deferred, "AUTH_REQUIRED deferred"),
    )


# ── orders ──────────────────────────────────────────────────────────────────────────────────

_ORDERS_SQL = """
SELECT order_uid, case_id, isin, side, order_type, quantity, limit_price_inr, state, broker,
       sleeve, staged_at, staged_for_date, executed_at, filled_quantity, avg_fill_price_inr,
       costs_inr, rejection_reason
FROM order_
ORDER BY staged_at DESC, id DESC
LIMIT %s
"""

_PAPER_ORDERS_SQL = """
SELECT DISTINCT ON (book_id) book_id, trading_date, recorded_at, orders
FROM paper_session
WHERE jsonb_array_length(orders) > 0
ORDER BY book_id, trading_date DESC
"""


def _ledger_line(row: Sequence[Any]) -> OrderLineOut:
    (uid, case_id, isin, side, order_type, quantity, limit_price, state, broker, sleeve, staged_at,
     staged_for, executed_at, filled, avg_price, costs, rejection) = row  # fmt: skip
    where = f"order_ {uid}"
    return OrderLineOut(
        source="order_",
        book=case_id,
        reference=uid,
        trading_date=staged_for,
        staged_at=staged_at,
        isin=isin,
        side=side,
        quantity=_whole(quantity, f"{where} quantity"),
        order_type=order_type,
        limit_price=_money(limit_price, f"{where} limit_price_inr"),
        state=state,
        tags=[broker, sleeve],
        filled_quantity=_whole(filled, f"{where} filled_quantity"),
        avg_fill_price_inr=_money(avg_price, f"{where} avg_fill_price_inr"),
        costs_inr=_money(costs, f"{where} costs_inr"),
        executed_at=executed_at,
        rejection_reason=_masked(rejection),
    )


def _paper_line(
    book_id: str, trading_date: date, recorded_at: datetime, document: Any
) -> OrderLineOut:
    if not isinstance(document, Mapping):
        raise StatusQueryError(f"paper_session {book_id} {trading_date}: order {document!r}")
    where = f"paper_session {book_id} {trading_date} order"
    tag = document.get("tag")
    return OrderLineOut(
        source="paper_session",
        book=book_id,
        reference=None,
        trading_date=trading_date,
        staged_at=recorded_at,
        isin=str(document["isin"]),
        side=str(document["side"]),
        quantity=_whole(document.get("quantity"), f"{where} quantity"),
        order_type=str(document.get("order_type") or "MARKET"),
        limit_price=_money(document.get("limit_price"), f"{where} limit_price"),
        # The session placed it on the paper SimBroker for the next session to fill; the ledger
        # row carries no fill of its own (that is the next session's book).
        state="PLACED",
        tags=["SIM"] + ([] if tag is None else [str(tag)]),
        filled_quantity=None,
        avg_fill_price_inr=None,
        costs_inr=None,
        executed_at=None,
        rejection_reason=None,
    )


def read_orders(conn: Connection, *, as_of: datetime, limit: int = ORDERS_LIMIT) -> OrdersOut:
    """The latest orders: the newest `order_` rows, and each paper book's newest placed orders.

    `order_` is the X1 ledger — staged, sent, filled, rail-blocked, rejected — with its broker and
    sleeve as tags. The paper session keeps its own orders in `paper_session.orders` (the requests
    it placed on the paper broker after A8 cleared them, with the caller's `tag`), so each paper
    book's newest session that placed any is listed beside the ledger. The M17 books' staged and
    refused orders are their journaled BUY/SELL/RAIL_BLOCK decisions (`/status/managers`).
    """
    ledger = [_ledger_line(row) for row in conn.execute(_ORDERS_SQL, (limit,)).fetchall()]
    paper: list[OrderLineOut] = []
    for book_id, trading_date, recorded_at, orders in conn.execute(_PAPER_ORDERS_SQL).fetchall():
        paper += [_paper_line(book_id, trading_date, recorded_at, doc) for doc in orders or []]
    return OrdersOut(as_of=as_of, limit=limit, ledger=ledger, paper=paper)


# ── the M17 mandates ────────────────────────────────────────────────────────────────────────


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def read_mandates(roster: Any) -> list[MandateOut]:
    """Each roster book's mandate summary, in roster order, from the roster `/status/managers`
    scores against.

    Reads the books' declared fields only (`model_dump`), so this module never imports the
    roster's types; a prompt path or prompt text is never part of the summary.
    """
    out: list[MandateOut] = []
    for book in roster.books:
        fields: Mapping[str, Any] = book.model_dump()
        models = fields.get("models") or {}
        manager = fields.get("manager") or fields.get("controls_for") or fields.get("style_for")
        out.append(
            MandateOut(
                book_id=str(fields["id"]),
                kind=str(fields["kind"]),
                manager=_text(manager),
                role=_text(fields.get("role")),
                style=_text(fields.get("style")),
                opening_capital_inr=_required_money(
                    fields.get("opening_capital_inr"), f"mandate {fields['id']} capital"
                ),
                max_positions=fields.get("max_positions"),
                max_position_pct=_money(fields.get("max_position_pct"), "max_position_pct"),
                max_sector_pct=_money(fields.get("max_sector_pct"), "max_sector_pct"),
                decision_model=_text(models.get("decision")),
            )
        )
    return out


# ── model cost ──────────────────────────────────────────────────────────────────────────────

_MODEL_COST_SQL = """
SELECT case_id,
       provider,
       model,
       (ts AT TIME ZONE 'Asia/Kolkata')::date AS day,
       count(*),
       sum(tokens_in),
       sum(tokens_out),
       sum(cost_inr),
       sum(cost_usd),
       count(cost_usd)
FROM token_usage
WHERE ts >= %s
GROUP BY case_id, provider, model, day
ORDER BY day DESC, case_id NULLS LAST, provider, model
"""


def model_cost_since(as_of: datetime, days: int = MODEL_COST_DAYS) -> datetime:
    """The window's first instant: IST midnight of the day `days - 1` before `as_of`'s IST day."""
    first = as_of.astimezone(IST).date() - timedelta(days=days - 1)
    return datetime.combine(first, time(0), tzinfo=IST)


def read_model_cost(
    conn: Connection, *, as_of: datetime, days: int = MODEL_COST_DAYS
) -> ModelCostOut:
    """Model spend from `token_usage` per manager (case), provider, model and IST day.

    Summed in the database over `numeric` columns and carried as `Decimal` to the page; the totals
    are summed here from those Decimals. `cost_inr` is the settled figure every row has;
    `cost_usd` is what the provider billed, when the row recorded it — a day with no USD figure
    says so (None) rather than showing $0.
    """
    since = model_cost_since(as_of, days)
    lines: list[ModelCostLineOut] = []
    for row in conn.execute(_MODEL_COST_SQL, (since,)).fetchall():
        case_id, provider, model, day, calls, tokens_in, tokens_out, inr, usd, usd_calls = row
        where = f"token_usage {case_id} {model} {day}"
        lines.append(
            ModelCostLineOut(
                case_id=case_id,
                provider=provider,
                model=model,
                day=day,
                calls=_whole(calls, f"{where} calls"),
                tokens_in=_whole(_whole_numeric(tokens_in), f"{where} tokens_in"),
                tokens_out=_whole(_whole_numeric(tokens_out), f"{where} tokens_out"),
                cost_inr=_required_money(inr, f"{where} cost_inr"),
                cost_usd=_money(usd, f"{where} cost_usd"),
                usd_calls=_whole(usd_calls, f"{where} usd_calls"),
            )
        )
    usd = [line.cost_usd for line in lines if line.cost_usd is not None]
    return ModelCostOut(
        as_of=as_of,
        days=days,
        since=since,
        lines=lines,
        total_inr=sum((line.cost_inr for line in lines), Decimal(0)),
        total_usd=sum(usd, Decimal(0)) if usd else None,
    )


def _whole_numeric(value: object) -> object:
    """``sum(bigint)`` is ``numeric`` in Postgres: an integral `Decimal` is a whole count."""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return value
