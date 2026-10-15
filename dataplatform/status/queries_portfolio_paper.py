"""D5: the strategy paper books on the console's Portfolio page (M18.7: every book a portfolio).

A strategy paper book — today the D13 momentum v2 book the ``paper_session`` job runs — is shown
exactly like an M17 book: its holdings with CMP, average price and the day's change, the orders
staged for the next session, and its fund totals. It is read from what the paper session persists
and nothing it does not:

* **which books** — every distinct ``paper_session.book_id`` that is not an M17 desk record
  (``m17_fund_managers``, ``m17_dry*_fund_managers``), so a future strategy book appears by itself;
* **what it holds and its cash** — the newest decided session's ``expected_book`` (M15.3), the
  book's own accounting side: ISIN, quantity, cost basis (buy charges included), and cash. A row
  recorded before M15.3 has none; the broker's own lots and cash in ``book_state.broker`` are
  read then, and the page says so;
* **what is staged** — ``book_state.broker.staged``: the orders the session placed, which fill on
  the next session;
* **the opening capital** — the first decided session's cash: no order can have filled by the end
  of a book's first session (an order fills the session after it is placed), so that cash is the
  capital the book opened with. Never a constant;
* **NAV** — the paper session journals no mark of its own, so NAV is the book's cash plus each
  holding at the session's NSE close (the same `SessionPrices` the M17 books are marked with). A
  holding with no close leaves NAV unknown, said plainly, never guessed.

Same contract as `status.queries`: plain SELECTs over an open connection, wire models out; nothing
here imports `backtest`, `execution` or `analyst`, opens a connection, reads the clock or writes.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from dataplatform.status.models_portfolio import (
    BookChoiceOut,
    BookGlanceOut,
    FundTotalsOut,
    HoldingRowOut,
    PaperBookOut,
    StagedAction,
)
from dataplatform.status.queries import StatusQueryError
from dataplatform.status.queries_portfolio import (
    SessionPrices,
    _decimal,
    _Held,
    _pct,
    _read_symbols,
    _row,
    _Staged,
    _whole,
)
from dataplatform.store.db import Connection

__all__ = [
    "PAPER_KIND",
    "MemoPrices",
    "PaperBook",
    "PaperFigures",
    "paper_choice",
    "paper_figures",
    "paper_glance",
    "paper_policy",
    "read_paper_books",
]

#: The picker kind of a strategy paper book.
PAPER_KIND: Final = "PAPER"
#: A paper book's policy, from its id's prefix (the paper session names its book for its policy
#: and its ratification). A prefix not listed here is shown as the id's words before ``_paper``.
_POLICIES: Final = (("momentum_v2_paper_", "momentum v2 (D13)"),)
_COMPLETED: Final = "COMPLETED"

# The M17 desk records are the desk's state, not strategy books (`queries_portfolio`).
_SESSIONS_SQL = """
SELECT book_id, trading_date, outcome, reason, rebalanced, book_state IS NOT NULL AS decided
FROM paper_session
WHERE book_id <> %s AND book_id NOT LIKE %s
ORDER BY book_id, trading_date
"""

_STATES_SQL = """
SELECT book_id, trading_date, book_state, expected_book FROM paper_session
WHERE book_id = ANY(%s) AND trading_date = ANY(%s) AND book_state IS NOT NULL
"""


@dataclass(frozen=True, slots=True)
class _Session:
    trading_date: date
    outcome: str
    reason: str
    rebalanced: bool
    decided: bool


@dataclass(frozen=True, slots=True)
class _State:
    """One decided session's book: holdings (ISIN → quantity, cost), cash, staged orders."""

    trading_date: date
    held: Mapping[str, tuple[int, Decimal]]
    cash: Decimal
    staged: Mapping[str, int]
    """ISIN → net staged quantity (BUY positive, SELL negative), zero nets dropped."""
    from_broker: bool
    """No accounting book was recorded: holdings and cash are the broker's own."""


@dataclass(frozen=True, slots=True)
class PaperBook:
    """One strategy paper book as the paper session recorded it."""

    book_id: str
    sessions: tuple[_Session, ...]
    first: _State | None
    previous: _State | None
    latest: _State | None

    @property
    def last_rebalance(self) -> date | None:
        done = [s.trading_date for s in self.sessions if s.rebalanced]
        return done[-1] if done else None

    @property
    def opening_capital(self) -> Decimal | None:
        return None if self.first is None else self.first.cash


def paper_policy(book_id: str) -> str:
    """The policy a paper book runs, for its label (``momentum v2 (D13)``)."""
    for prefix, label in _POLICIES:
        if book_id.startswith(prefix):
            return label
    stem, found, _ = book_id.partition("_paper")
    return stem.replace("_", " ") if found and stem else book_id


def _state(book_id: str, on: date, state: object, expected: object) -> _State:
    where = f"paper_session {book_id} {on}"
    if not isinstance(state, Mapping) or not isinstance(state.get("broker"), Mapping):
        raise StatusQueryError(f"{where}: book_state has no broker state")
    broker: Mapping[str, Any] = state["broker"]
    held: dict[str, tuple[int, Decimal]] = {}

    def add(isin: str, quantity: int, cost: Decimal) -> None:
        if quantity > 0:
            before = held.get(isin, (0, Decimal(0)))
            held[isin] = (before[0] + quantity, before[1] + cost)

    if expected is not None:
        if not isinstance(expected, Mapping):
            raise StatusQueryError(f"{where}: expected_book is {expected!r}")
        for position in expected.get("positions") or []:
            isin = str(position["isin"])
            add(
                isin,
                _whole(position.get("quantity"), f"{where} {isin} quantity"),
                _decimal(position.get("cost_basis"), f"{where} {isin} cost_basis"),
            )
        cash = _decimal(expected.get("cash"), f"{where} expected_book cash")
    else:
        # Before M15.3 no accounting book was recorded: the broker's settled and pending lots.
        for lot in [*(broker.get("holdings") or []), *(broker.get("pending") or [])]:
            isin = str(lot["isin"])
            add(
                isin,
                _whole(lot.get("quantity"), f"{where} {isin} lot quantity"),
                _decimal(lot.get("cost"), f"{where} {isin} lot cost"),
            )
        cash = _decimal(broker.get("cash"), f"{where} broker cash")
    nets: dict[str, int] = {}
    for order in broker.get("staged") or []:
        isin = str(order["isin"])
        quantity = _whole(order.get("quantity"), f"{where} staged {isin} quantity")
        side = order.get("side")
        if side not in ("BUY", "SELL"):
            raise StatusQueryError(f"{where}: staged order {isin} has side {side!r}")
        nets[isin] = nets.get(isin, 0) + (quantity if side == "BUY" else -quantity)
    return _State(
        trading_date=on,
        held=held,
        cash=cash,
        staged={isin: net for isin, net in nets.items() if net},
        from_broker=expected is None,
    )


def read_paper_books(conn: Connection, *, live_record: str, dry_pattern: str) -> list[PaperBook]:
    """Every strategy paper book in ``paper_session``, by id: its sessions, and the state of its
    first, previous and newest decided sessions (two SELECTs whatever the book's age)."""
    by_book: dict[str, list[_Session]] = {}
    for book_id, on, outcome, reason, rebalanced, decided in conn.execute(
        _SESSIONS_SQL, (live_record, dry_pattern)
    ).fetchall():
        by_book.setdefault(str(book_id), []).append(
            _Session(on, str(outcome), str(reason), bool(rebalanced), bool(decided))
        )
    wanted: dict[str, list[date]] = {}
    for book_id, sessions in by_book.items():
        decided = [s.trading_date for s in sessions if s.decided]
        wanted[book_id] = sorted({*decided[:1], *decided[-2:]})
    states: dict[tuple[str, date], _State] = {}
    days = sorted({d for ds in wanted.values() for d in ds})
    if days:
        for book_id, on, state, expected in conn.execute(
            _STATES_SQL, (sorted(wanted), days)
        ).fetchall():
            if on in wanted.get(str(book_id), ()):
                states[(str(book_id), on)] = _state(str(book_id), on, state, expected)
    out: list[PaperBook] = []
    for book_id in sorted(by_book):
        decided = [s.trading_date for s in by_book[book_id] if s.decided]
        out.append(
            PaperBook(
                book_id=book_id,
                sessions=tuple(by_book[book_id]),
                first=states.get((book_id, decided[0])) if decided else None,
                previous=states.get((book_id, decided[-2])) if len(decided) > 1 else None,
                latest=states.get((book_id, decided[-1])) if decided else None,
            )
        )
    return out


def paper_choice(book: PaperBook) -> BookChoiceOut:
    """The picker's entry for a paper book."""
    return BookChoiceOut(
        book_id=book.book_id,
        kind=PAPER_KIND,
        manager=None,
        opening_capital_inr=book.opening_capital or Decimal(0),
        policy=paper_policy(book.book_id),
    )


# ── the figures ──────────────────────────────────────────────────────────────────────────────


class MemoPrices:
    """`SessionPrices` that reads each (session, names) once per page — the picker's glance and
    the page's own figures ask for the same closes."""

    def __init__(self, prices: SessionPrices) -> None:
        self._prices = prices
        self._seen: dict[tuple[date, frozenset[str]], Mapping[str, Decimal]] = {}

    def closes(self, session: date, isins: Collection[str]) -> Mapping[str, Decimal]:
        key = (session, frozenset(isins))
        if key not in self._seen:
            self._seen[key] = self._prices.closes(session, key[1]) if key[1] else {}
        return self._seen[key]

    def previous_session(self, session: date) -> date | None:
        return self._prices.previous_session(session)

    def next_session(self, session: date) -> date | None:
        return self._prices.next_session(session)


def _nav(state: _State, prices: SessionPrices) -> Decimal | None:
    closes = prices.closes(state.trading_date, state.held)
    if any(isin not in closes for isin in state.held):
        return None
    return state.cash + sum(
        (quantity * closes[isin] for isin, (quantity, _) in state.held.items()), Decimal(0)
    )


@dataclass(frozen=True, slots=True)
class PaperFigures:
    """What the page shows for a paper book: rows, totals, the paper line, notes."""

    session: date | None
    next_session: date | None
    rows: list[HoldingRowOut]
    totals: FundTotalsOut | None
    paper: PaperBookOut
    missing: list[str]


def _next_rebalance(book: PaperBook, session: date, prices: SessionPrices) -> date | None:
    """The first session of the month after the last rebalance's (the ratified monthly cycle); the
    next session when this month has not rebalanced yet (a red first session moves it there)."""
    last = book.last_rebalance
    if last is None or (last.year, last.month) != (session.year, session.month):
        return prices.next_session(session)
    first = date(session.year + session.month // 12, session.month % 12 + 1, 1)
    return prices.next_session(first - timedelta(days=1))


def paper_glance(book: PaperBook, prices: SessionPrices) -> BookGlanceOut:
    """A paper book at a glance, for the picker."""
    latest, previous = book.latest, book.previous
    nav = None if latest is None else _nav(latest, prices)
    before = None if previous is None else _nav(previous, prices)
    change = None
    if nav is not None and before is not None and before != 0:
        change = (nav - before) / before * Decimal(100)
    return BookGlanceOut(
        book_id=book.book_id,
        session=None if latest is None or nav is None else latest.trading_date,
        nav=nav,
        daily_change_pct=change,
        staged=latest is not None and bool(latest.staged),
    )


def paper_figures(conn: Connection, book: PaperBook, prices: SessionPrices) -> PaperFigures:
    """The holdings, the staged orders and the fund totals of ``book``'s newest decided session.

    Each row is built by `queries_portfolio`'s own row builder, so a paper book's row is computed
    exactly as an M17 book's. A session the book did not decide (red) is named, never hidden.
    """
    missing: list[str] = []
    newest = book.sessions[-1] if book.sessions else None
    latest = book.latest
    if newest is not None and not newest.decided:
        missing.append(
            f"{newest.trading_date.isoformat()}: {newest.outcome} ({newest.reason}) — the book was "
            "not decided that session"
            + ("" if latest is None else f"; shown as decided on {latest.trading_date.isoformat()}")
            + "."
        )
    elif newest is not None and newest.outcome != _COMPLETED:
        # A RECON_BREAK records the book as the fills left it, and refuses later sessions.
        missing.append(
            f"{newest.trading_date.isoformat()}: {newest.outcome} ({newest.reason}) — the book is "
            "shown as that session left it."
        )
    if latest is None:
        missing.append(f"No session of {book.book_id} has been decided yet.")
        info = PaperBookOut(
            policy=paper_policy(book.book_id),
            sessions=len(book.sessions),
            decided_sessions=0,
            first_session=None,
            last_rebalance=book.last_rebalance,
            next_rebalance=None,
            holdings_from="none",
        )
        return PaperFigures(None, None, [], None, info, missing)
    session = latest.trading_date
    info = PaperBookOut(
        policy=paper_policy(book.book_id),
        sessions=len(book.sessions),
        decided_sessions=sum(1 for s in book.sessions if s.decided),
        first_session=None if book.first is None else book.first.trading_date,
        last_rebalance=book.last_rebalance,
        next_rebalance=_next_rebalance(book, session, prices),
        holdings_from="broker" if latest.from_broker else "expected_book",
    )
    if latest.from_broker:
        missing.append(
            f"{session.isoformat()} recorded no accounting book (expected_book, M15.3): holdings "
            "and cash are the paper broker's own."
        )
    names = set(latest.held) | set(latest.staged)
    previous_session = prices.previous_session(session)
    if previous_session is None and names:
        missing.append(
            f"The trading calendar names no session before {session.isoformat()}: no day's change."
        )
    closes = prices.closes(session, names)
    previous_closes = {} if previous_session is None else prices.closes(previous_session, names)
    symbols = _read_symbols(conn, names)
    ordered = sorted(latest.held) + sorted(names - set(latest.held))
    rows: list[HoldingRowOut] = []
    for isin in ordered:
        net = latest.staged.get(isin, 0)
        held = latest.held.get(isin)
        rows.append(
            _row(
                isin,
                symbol=symbols.get(isin),
                held=None if held is None else _Held(*held),
                staged=None if not net else _Staged(buy=max(net, 0), sell=max(-net, 0)),
                suspended=None,
                session=session,
                cmp=closes.get(isin),
                previous_session=previous_session,
                previous_close=previous_closes.get(isin),
            )
        )
    for row in rows:
        if not row.held and row.action is not StagedAction.BUY:
            missing.append(
                f"{row.isin}: a {row.action} is staged for a name the book does not hold."
            )
    held_rows = [r for r in rows if r.held]
    values = [r.value for r in held_rows]
    current = sum((v for v in values if v is not None), Decimal(0))
    if any(v is None for v in values):
        missing.append(
            f"A holding has no close on {session.isoformat()}, so the book's NAV is unknown."
        )
        return PaperFigures(session, prices.next_session(session), rows, None, info, missing)
    nav = latest.cash + current
    opening = book.opening_capital or Decimal(0)
    previous = book.previous
    previous_nav = None if previous is None else _nav(previous, prices)
    totals = FundTotalsOut(
        session=session,
        opening_capital=opening,
        invested=sum((r.cost for r in held_rows if r.cost is not None), Decimal(0)),
        current_value=current,
        cash=latest.cash,
        nav=nav,
        total_profit=nav - opening,
        total_profit_pct=_pct(nav - opening, opening) or Decimal(0),
        previous_session=None
        if previous is None or previous_nav is None
        else previous.trading_date,
        previous_nav=previous_nav,
        daily_change=None if previous_nav is None else nav - previous_nav,
        daily_change_pct=None if previous_nav is None else _pct(nav - previous_nav, previous_nav),
        positions=len(held_rows),
        unreconciled=None,
        reconciled=True,
    )
    return PaperFigures(session, prices.next_session(session), rows, totals, info, missing)
