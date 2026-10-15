"""D5: the reads behind the console's Portfolio page (M18.7) — one M17 book as a portfolio.

Every book the M17 desk records (a manager's primary and its mirror, a control, a style book,
the bench) is shown the same way, from what the M17 job persists and nothing it does not:

* **the book's marks** — its ``BOOK_MARK`` journal lines, parsed by
  `analyst.fundmanager.scoreboard.inputs_from_journal`, the parse the scoreboard, the digest and
  `/status/managers` use. NAV, cash and the position count are the mark's, never recomputed;
* **what it holds** — the desk's own record of the session (`paper_session.book_state`, the row
  `backtest.fm_job` writes per stream: ``m17_fund_managers`` live, ``m17_dry*_fund_managers``
  dry), whose ``account.expected_book`` is the book's accounting side: ISIN, quantity and the
  cost basis (buy charges included) of each holding;
* **what is staged** — the book's staging lines on its latest marked session: a ``BUY``/``SELL``
  entry carrying an ``order_uid``, which `FundBook._stage` writes and nothing else does
  (``STAGED`` for a decision or a control, ``MIRROR_FOLLOW`` for a mirror, ``STOP_EXIT`` for a
  mechanical stop, a pending exit's child). They fill on the next session, which is not marked
  yet, so none of them has filled;
* **suspended holdings** — the book's ``SUSPENDED_HOLDING`` lines on that session (M17.13),
  whose ``last_close`` is what the book was marked at;
* **the close the book was marked at** — the session's NSE L1 close (EQ, then a held name's
  BE/BZ print), read the way the desk's market reads it (`backtest.fm_world._LakeBookMarket`),
  with the D22 price quarantine applied. The previous session comes from the trading calendar.

**Strategy paper books** (the D13 momentum v2 book, and any later one) are read from their own
``paper_session`` rows by `queries_portfolio_paper` and shown with the same rows and totals.

**Streams.** Until S0 the desk writes only the dry stream. The page shows the live stream when it
holds any entry for the roster's books, and the dry stream otherwise, labelled so.

Same contract as `status.queries`: every function takes an open connection (or a reader it is
handed) and returns wire models; none opens a database connection, reads the clock, or writes.
`analyst` is imported lazily inside the functions that need its parsers, as `status.queries`
does for `/status/managers`, so importing the status API stays free of System 2.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Protocol

from dataplatform.redaction import mask_and_truncate
from dataplatform.status.models_portfolio import (
    BookChoiceOut,
    BookGlanceOut,
    FundTotalsOut,
    HoldingRowOut,
    PortfolioOut,
    PortfolioStream,
    StagedAction,
)
from dataplatform.status.queries import StatusQueryError
from dataplatform.status.queries_desk import read_mandates
from dataplatform.store.db import Connection

__all__ = [
    "DRY_DESK_PATTERN",
    "LIVE_DESK_RECORD",
    "LakeSessionPrices",
    "SessionPrices",
    "UnknownBookError",
    "choose_stream",
    "read_m17_dry_journal",
    "read_portfolio",
]

#: The `paper_session.book_id` the live desk records its state under (`backtest.fm_job.DESK_IDS`).
LIVE_DESK_RECORD: Final = "m17_fund_managers"
#: The dry desk's records: ``m17_dry_fund_managers`` (the first roster) and
#: ``m17_dry2_fund_managers`` (M17.14). The newest one holding the session is read.
DRY_DESK_PATTERN: Final = r"m17\_dry%\_fund\_managers"
#: The dry stream's ``case_id`` prefix (`analyst.fundmanager.job.StreamJournal`).
_DRY_PREFIX: Final = "m17-dry:"

_HUNDRED: Final = Decimal(100)
_ERROR_CHARS: Final = 1000
#: The journal ``payload.event`` of a suspended holding's line (`analyst.fundmanager.books`).
_SUSPENDED_EVENT: Final = "SUSPENDED_HOLDING"
#: The NSE trade-for-trade series a held name may print in when it leaves EQ (`backtest.run`).
_RESTRICTED_SERIES: Final = ("BE", "BZ")
_PRICES_DATASET: Final = "prices_raw"


class UnknownBookError(LookupError):
    """The book asked for is not one the M17 roster records — the page answers 404."""


# ── the close the book was marked at ────────────────────────────────────────────────────────


class SessionPrices(Protocol):
    """The session closes a book is marked at, and the calendar around a session."""

    def closes(self, session: date, isins: Collection[str]) -> Mapping[str, Decimal]:
        """The close of each of ``isins`` that printed on ``session`` (absent: no print)."""
        ...

    def previous_session(self, session: date) -> date | None: ...

    def next_session(self, session: date) -> date | None: ...


class LakeSessionPrices:
    """`SessionPrices` over the L1 lake and the NSE trading calendar.

    What it does: reads one session's ``prices_raw`` partition, NSE EQ first and then, for a name
    with no EQ print, its BE/BZ print (BE before BZ) — the order `backtest.fm_world` marks a
    holding in — with the D22 quarantine's predicate. Money comes back as `Decimal` off the lake's
    decimal columns.
    What it never does: read past the session it is asked about, or a BSE bar.
    """

    def __init__(self, data_root: Path, *, calendar: Any = None) -> None:
        self._data_root = data_root
        self._calendar = calendar

    def _sessions(self) -> Any:
        if self._calendar is None:
            from dataplatform.ingest.calendar import trading_calendar

            self._calendar = trading_calendar()
        return self._calendar

    def closes(self, session: date, isins: Collection[str]) -> Mapping[str, Decimal]:
        from dataplatform.query.price_quarantine import default_price_quarantine
        from dataplatform.store.l2 import open_connection
        from dataplatform.store.paths import l1_partition_path

        wanted = sorted(set(isins))
        path = l1_partition_path(_PRICES_DATASET, session, data_root=self._data_root)
        if not wanted or not path.exists():
            return {}
        admits = default_price_quarantine().sql_admits()
        con = open_connection()
        try:
            rows = con.execute(
                "SELECT isin, series, close FROM read_parquet($path) "
                "WHERE exchange = 'NSE' AND series IN ('EQ', 'BE', 'BZ') AND close > 0 "
                f"AND isin IN (SELECT unnest($isins)) AND {admits} ORDER BY isin, series",
                {"path": str(path), "isins": wanted},
            ).fetchall()
        finally:
            con.close()
        rank = {"EQ": 0, _RESTRICTED_SERIES[0]: 1, _RESTRICTED_SERIES[1]: 2}
        best: dict[str, tuple[int, Decimal]] = {}
        for isin, series, close in rows:
            order = rank[str(series)]
            if str(isin) not in best or order < best[str(isin)][0]:
                best[str(isin)] = (order, _decimal(close, f"L1 close {isin} {session}"))
        return {isin: close for isin, (_, close) in best.items()}

    def _around(self, start: date, end: date) -> list[date] | None:
        from dataplatform.ingest.calendar import CalendarError

        try:
            return list(self._sessions().expected_sessions(start, end))
        except CalendarError:
            return None  # outside the calendar's coverage: the page says the session is unknown

    def previous_session(self, session: date) -> date | None:
        days = self._around(session - timedelta(days=21), session) or []
        earlier = [day for day in days if day < session]
        return earlier[-1] if earlier else None

    def next_session(self, session: date) -> date | None:
        days = self._around(session, session + timedelta(days=21)) or []
        later = [day for day in days if day > session]
        return later[0] if later else None


def _decimal(value: object, what: str) -> Decimal:
    """An exact `Decimal` from a `Decimal` or a decimal string — never a float (status rule)."""
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


def _whole(value: object, what: str) -> int:
    if isinstance(value, bool):
        raise StatusQueryError(f"{what}: {value!r} is not a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    raise StatusQueryError(f"{what}: {value!r} is not a whole number")


# ── the journal streams ─────────────────────────────────────────────────────────────────────


class _NoSink:
    """`StreamJournal` needs a sink to file into; the page only ever reads, so it has none."""

    def append(self, entry: object, *, evidence: object = None) -> object:
        raise AssertionError("the console never writes to the journal")


def read_m17_dry_journal(conn: Connection, *, data_root: Path, roster: Any) -> tuple[Any, ...]:
    """Every dry-stream entry of the roster's books, in append order, read back as the books
    wrote them (``StreamJournal.unprefixed``: ``case_id`` without ``m17-dry:``, no
    ``payload.stream``) — so the scoreboard's own parsers read them exactly as the dry digest did.

    Read through `analyst.journal.Journal` like `status.queries.read_m17_journal`, lazily.
    """
    from analyst.fundmanager.job import STREAM_DRY, StreamJournal
    from analyst.journal import EvidenceStore, Journal, JournalFilter

    journal = Journal(conn, evidence=EvidenceStore(data_root / "evidence"))
    stream = StreamJournal(_NoSink(), STREAM_DRY)
    entries = [
        e
        for book in roster.books
        for e in journal.entries(JournalFilter(case_id=stream.case_id(book.id)))
    ]
    return tuple(stream.unprefixed(e) for e in sorted(entries, key=lambda e: e.id))


def choose_stream(live: Sequence[Any], dry: Sequence[Any]) -> tuple[PortfolioStream, Sequence[Any]]:
    """The live stream when it holds any entry, else the dry stream (labelled DRY RUN)."""
    if live:
        return PortfolioStream.LIVE, live
    return PortfolioStream.DRY, dry


# ── the desk's record and the symbols ───────────────────────────────────────────────────────

_LIVE_DESK_SQL = """
SELECT book_id, book_state FROM paper_session
WHERE book_id = %s AND trading_date = %s
"""

_DRY_DESK_SQL = """
SELECT book_id, book_state FROM paper_session
WHERE book_id LIKE %s AND trading_date = %s
ORDER BY recorded_at DESC, book_id DESC
LIMIT 1
"""

_SYMBOLS_SQL = """
SELECT DISTINCT ON (isin) isin, symbol FROM symbol_history
WHERE isin = ANY(%s)
ORDER BY isin, (exchange = 'NSE') DESC, (valid_to IS NULL) DESC, valid_from DESC
"""


@dataclass(frozen=True, slots=True)
class _DeskRecord:
    record_id: str | None
    state: Mapping[str, Any] | None


def _read_desk(conn: Connection, stream: PortfolioStream, session: date) -> _DeskRecord:
    if stream is PortfolioStream.LIVE:
        row = conn.execute(_LIVE_DESK_SQL, (LIVE_DESK_RECORD, session)).fetchone()
    else:
        row = conn.execute(_DRY_DESK_SQL, (DRY_DESK_PATTERN, session)).fetchone()
    if row is None:
        return _DeskRecord(None, None)
    record_id, state = row
    if state is not None and not isinstance(state, Mapping):
        raise StatusQueryError(f"paper_session {record_id} {session}: book_state is {state!r}")
    return _DeskRecord(str(record_id), state)


def _read_symbols(conn: Connection, isins: Collection[str]) -> dict[str, str]:
    if not isins:
        return {}
    rows = conn.execute(_SYMBOLS_SQL, (sorted(isins),)).fetchall()
    return {str(isin): str(symbol) for isin, symbol in rows}


# ── the book ────────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Held:
    quantity: int
    cost: Decimal


@dataclass(slots=True)
class _Staged:
    buy: int = 0
    sell: int = 0


@dataclass(slots=True)
class _BookLines:
    """The book's own journal lines the page reads, on its latest marked session."""

    staged: dict[str, _Staged] = field(default_factory=dict)
    suspended: dict[str, tuple[date, Decimal]] = field(default_factory=dict)


def _book_lines(entries: Sequence[Any], book_id: str, session: date) -> _BookLines:
    out = _BookLines()
    for entry in entries:
        if entry.case_id != book_id or entry.trading_date != session or entry.isin is None:
            continue
        payload = entry.payload
        where = f"{book_id} {session} {entry.isin}"
        if entry.decision in ("BUY", "SELL") and "order_uid" in payload:
            quantity = _whole(payload.get("quantity"), f"staging line {where} quantity")
            staged = out.staged.setdefault(entry.isin, _Staged())
            if entry.decision == "BUY":
                staged.buy += quantity
            else:
                staged.sell += quantity
        elif payload.get("event") == _SUSPENDED_EVENT:
            out.suspended[entry.isin] = (
                date.fromisoformat(str(payload["last_trade_date"])),
                _decimal(payload.get("last_close"), f"suspended line {where} last_close"),
            )
    return out


def _held(state: Mapping[str, Any] | None, book_id: str, where: str) -> dict[str, _Held] | None:
    """The book's holdings from the desk record's accounting book; None when it has none."""
    if state is None:
        return None
    books = state.get("books") or {}
    book = books.get(book_id)
    if book is None:
        return None
    expected = (book.get("account") or {}).get("expected_book")
    if not isinstance(expected, Mapping):
        raise StatusQueryError(f"{where}: {book_id} has no account.expected_book")
    out: dict[str, _Held] = {}
    for position in expected.get("positions") or []:
        isin = str(position["isin"])
        quantity = _whole(position.get("quantity"), f"{where} {book_id} {isin} quantity")
        if quantity > 0:
            out[isin] = _Held(
                quantity, _decimal(position.get("cost_basis"), f"{where} {isin} cost_basis")
            )
    return out


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return None if whole == 0 else part / whole * _HUNDRED


def _row(
    isin: str,
    *,
    symbol: str | None,
    held: _Held | None,
    staged: _Staged | None,
    suspended: tuple[date, Decimal] | None,
    session: date,
    cmp: Decimal | None,
    previous_session: date | None,
    previous_close: Decimal | None,
) -> HoldingRowOut:
    action: StagedAction | None = None
    action_quantity: int | None = None
    if staged is not None:
        # A name with both a buy and a sell staged nets them: the page shows the side that moves.
        net = staged.buy - staged.sell
        if net > 0:
            action, action_quantity = StagedAction.BUY, net
        elif net < 0:
            action, action_quantity = StagedAction.SELL, -net
    notes: list[str] = []
    if suspended is not None:
        since, last_close = suspended
        value = None if held is None else held.quantity * last_close
        return HoldingRowOut(
            isin=isin,
            symbol=symbol,
            held=held is not None,
            quantity=None if held is None else held.quantity,
            cmp=None,
            cmp_session=None,
            previous_close=None,
            previous_session=None,
            day_change=None,
            position_day_change=None,
            day_change_pct=None,
            cost=None if held is None else held.cost,
            average_price=None if held is None else held.cost / held.quantity,
            value=value,
            profit=None,
            profit_pct=None,
            suspended_since=since,
            action=action,
            action_quantity=action_quantity,
            estimated_cost=None,
            note=f"no CMP: valued at its last close {last_close} ({since.isoformat()})",
        )
    if cmp is None:
        notes.append(f"no close on {session.isoformat()}")
    day_change = None
    if cmp is not None and previous_close is not None:
        day_change = cmp - previous_close
    elif previous_session is not None and previous_close is None:
        notes.append(f"no close on {previous_session.isoformat()}")
    value = None if held is None or cmp is None else held.quantity * cmp
    profit = None if held is None or value is None else value - held.cost
    return HoldingRowOut(
        isin=isin,
        symbol=symbol,
        held=held is not None,
        quantity=None if held is None else held.quantity,
        cmp=cmp,
        cmp_session=None if cmp is None else session,
        previous_close=previous_close,
        previous_session=previous_session,
        day_change=day_change,
        position_day_change=(
            None if day_change is None or held is None else day_change * held.quantity
        ),
        day_change_pct=(
            None
            if day_change is None or previous_close is None
            else _pct(day_change, previous_close)
        ),
        cost=None if held is None else held.cost,
        average_price=None if held is None else held.cost / held.quantity,
        value=value,
        profit=profit,
        profit_pct=None if profit is None or held is None else _pct(profit, held.cost),
        suspended_since=None,
        action=action,
        action_quantity=action_quantity,
        estimated_cost=(
            None
            if action is not StagedAction.BUY or action_quantity is None or cmp is None
            else action_quantity * cmp
        ),
        note="; ".join(notes) or None,
    )


def _choice(mandate: Any) -> BookChoiceOut:
    return BookChoiceOut(
        book_id=mandate.book_id,
        kind=mandate.kind,
        manager=mandate.manager,
        opening_capital_inr=mandate.opening_capital_inr,
    )


def book_choices(roster: Any) -> list[BookChoiceOut]:
    """Every book the roster records, in roster order — each selectable on its own."""
    return [_choice(m) for m in read_mandates(roster)]


def default_book(choices: Sequence[BookChoiceOut]) -> BookChoiceOut:
    """The first manager book (the roster's first book when it has none)."""
    if not choices:
        raise UnknownBookError("the M17 roster records no book")
    return next((c for c in choices if c.kind == "MANAGER"), choices[0])


def _glances(
    entries: Sequence[Any], marks: Sequence[Any], choices: Sequence[BookChoiceOut]
) -> list[BookGlanceOut]:
    """Every book at a glance from its marks and, in one pass over ``entries``, its staging.

    A book is marked as staged when a name nets to a BUY or a SELL on its latest marked session
    — the same rule the holdings table uses for the action column.
    """
    by_book: dict[str, list[Any]] = {}
    for mark in marks:
        by_book.setdefault(mark.book_id, []).append(mark)
    latest: dict[str, tuple[Any, Any]] = {}
    for book_id, book_marks in by_book.items():
        book_marks.sort(key=lambda m: m.session)
        latest[book_id] = (book_marks[-1], book_marks[-2] if len(book_marks) > 1 else None)
    nets: dict[tuple[str, str], int] = {}
    for entry in entries:
        found = latest.get(entry.case_id)
        if found is None or entry.trading_date != found[0].session or entry.isin is None:
            continue
        if entry.decision in ("BUY", "SELL") and "order_uid" in entry.payload:
            quantity = _whole(
                entry.payload.get("quantity"),
                f"staging line {entry.case_id} {entry.trading_date} {entry.isin} quantity",
            )
            key = (entry.case_id, entry.isin)
            nets[key] = nets.get(key, 0) + (quantity if entry.decision == "BUY" else -quantity)
    staged = {book_id for (book_id, _), net in nets.items() if net}
    out: list[BookGlanceOut] = []
    for choice in choices:
        mark, previous = latest.get(choice.book_id, (None, None))
        out.append(
            BookGlanceOut(
                book_id=choice.book_id,
                session=None if mark is None else mark.session,
                nav=None if mark is None else mark.nav,
                daily_change_pct=None
                if mark is None or previous is None
                else _pct(mark.nav - previous.nav, previous.nav),
                staged=choice.book_id in staged,
            )
        )
    return out


def read_portfolio(
    conn: Connection,
    *,
    entries: Sequence[Any],
    stream: PortfolioStream,
    roster: Any,
    book_id: str | None,
    prices: SessionPrices,
    as_of: datetime,
) -> PortfolioOut:
    """The Portfolio page for ``book_id`` (default: the first manager book) from ``stream``.

    ``entries`` are that stream's entries for the roster's books, as the books wrote them. Every
    strategy paper book in ``paper_session`` is selectable too, read from its own rows
    (`queries_portfolio_paper`). Raises `UnknownBookError` for a book neither records. A journal
    the scoreboard's parse refuses is reported in ``error`` with no figures, never papered over.
    """
    from analyst.fundmanager.scoreboard import ScoreboardError, inputs_from_journal
    from dataplatform.status.queries_portfolio_paper import (
        PAPER_KIND,
        MemoPrices,
        paper_choice,
        paper_figures,
        paper_glance,
        read_paper_books,
    )

    prices = MemoPrices(prices)
    paper_books = {
        b.book_id: b
        for b in read_paper_books(conn, live_record=LIVE_DESK_RECORD, dry_pattern=DRY_DESK_PATTERN)
    }
    desk_choices = book_choices(roster)
    choices = desk_choices + [paper_choice(b) for b in paper_books.values()]
    if book_id is None:
        book = default_book(choices)
    else:
        found = [c for c in choices if c.book_id == book_id]
        if not found:
            raise UnknownBookError(
                f"no M17 book and no strategy paper book {book_id!r} is recorded"
            )
        book = found[0]

    def page(**fields: Any) -> PortfolioOut:
        base: dict[str, Any] = {
            "as_of": as_of, "stream": stream, "desk_record": None, "book": book,
            "books": choices, "session": None, "next_session": None, "rows": [], "totals": None,
            "bench_note": None, "missing": [], "error": None,
        }  # fmt: skip
        return PortfolioOut(**{**base, **fields})

    paper_glances = [paper_glance(b, prices) for b in paper_books.values()]
    try:
        inputs = inputs_from_journal(entries, roster)
    except ScoreboardError as error:
        if book.kind != PAPER_KIND:
            return page(glances=paper_glances, error=mask_and_truncate(str(error), _ERROR_CHARS))
        inputs = None  # the M17 journal is not this book's: its picks just show no glance
    glances = [] if inputs is None else _glances(entries, inputs.marks, desk_choices)
    glances += paper_glances

    def page_with(**fields: Any) -> PortfolioOut:
        return page(glances=glances, **fields)

    if book.kind == PAPER_KIND:
        figures = paper_figures(conn, paper_books[book.book_id], prices)
        return page_with(
            stream=PortfolioStream.PAPER,
            desk_record=book.book_id,
            session=figures.session,
            next_session=figures.next_session,
            rows=figures.rows,
            totals=figures.totals,
            paper=figures.paper,
            missing=figures.missing,
        )
    assert inputs is not None
    marks = sorted((m for m in inputs.marks if m.book_id == book.book_id), key=lambda m: m.session)
    if not marks:
        return page_with(
            missing=[
                f"No session has been marked yet for {book.book_id} in the {stream.value} stream."
            ]
        )
    mark = marks[-1]
    previous = marks[-2] if len(marks) > 1 else None
    session = mark.session
    desk = _read_desk(conn, stream, session)
    where = f"paper_session {desk.record_id} {session}"
    missing: list[str] = []
    totals_kw: dict[str, Any] = {
        "session": session,
        "opening_capital": book.opening_capital_inr,
        "cash": mark.cash,
        "nav": mark.nav,
        "total_profit": mark.nav - book.opening_capital_inr,
        "total_profit_pct": _pct(mark.nav - book.opening_capital_inr, book.opening_capital_inr)
        or Decimal(0),
        "previous_session": None if previous is None else previous.session,
        "previous_nav": None if previous is None else previous.nav,
        "daily_change": None if previous is None else mark.nav - previous.nav,
        "daily_change_pct": None
        if previous is None
        else _pct(mark.nav - previous.nav, previous.nav),
        "positions": mark.positions,
    }

    if book.kind == "BENCH":
        bench = None if desk.state is None else desk.state.get("bench")
        note = "The bench holds its index (total return) as one position — no instrument, no ISIN."
        if isinstance(bench, Mapping) and bench.get("base_session"):
            note += (
                f" Bought with its opening capital at the {bench['base_session']} close, index "
                f"level {bench.get('base_level')}; marked at the index level each session."
            )
        invested = book.opening_capital_inr if mark.positions else Decimal(0)
        totals = FundTotalsOut(
            **totals_kw,
            invested=invested,
            current_value=mark.nav - mark.cash,
            unreconciled=None,
            reconciled=True,
        )
        return page_with(
            desk_record=desk.record_id,
            session=session,
            next_session=prices.next_session(session),
            totals=totals,
            bench_note=note,
        )

    held = _held(desk.state, book.book_id, where)
    if held is None:
        held = {}
        if mark.positions:
            missing.append(
                f"No desk record holds {book.book_id} on {session.isoformat()}, so its "
                f"{mark.positions} position(s) cannot be listed."
            )
    lines = _book_lines(entries, book.book_id, session)
    names = set(held) | {isin for isin, s in lines.staged.items() if s.buy != s.sell}
    previous_session = prices.previous_session(session)
    if previous_session is None and names:
        missing.append(
            f"The trading calendar names no session before {session.isoformat()}: no day's change."
        )
    closes = prices.closes(session, names)
    previous_closes = {} if previous_session is None else prices.closes(previous_session, names)
    symbols = _read_symbols(conn, names)
    ordered = sorted(held) + sorted(names - set(held))
    rows = [
        _row(
            isin,
            symbol=symbols.get(isin),
            held=held.get(isin),
            staged=lines.staged.get(isin),
            suspended=lines.suspended.get(isin) if isin in held else None,
            session=session,
            cmp=closes.get(isin),
            previous_session=previous_session,
            previous_close=previous_closes.get(isin),
        )
        for isin in ordered
    ]
    held_rows = [r for r in rows if r.held]
    values = [r.value for r in held_rows]
    known = [v for v in values if v is not None]
    current = sum(known, Decimal(0)) if len(known) == len(values) else None
    unreconciled = None if current is None else current + mark.cash - mark.nav
    reconciled = unreconciled == 0 and len(held_rows) == mark.positions
    if not reconciled:
        missing.append(
            f"The holdings listed ({len(held_rows)}) do not add up to the book's mark on "
            f"{session.isoformat()} ({mark.positions} position(s), NAV {mark.nav}); the mark's "
            "NAV and cash are shown as the book's figures."
        )
    for row in rows:
        if not row.held and row.action is not StagedAction.BUY:
            missing.append(
                f"{row.isin}: a {row.action} is staged for a name {book.book_id} does not hold."
            )
    totals = FundTotalsOut(
        **totals_kw,
        invested=sum((r.cost for r in held_rows if r.cost is not None), Decimal(0)),
        current_value=current,
        unreconciled=None if unreconciled == 0 else unreconciled,
        reconciled=reconciled,
    )
    return page_with(
        desk_record=desk.record_id,
        session=session,
        next_session=prices.next_session(session),
        rows=rows,
        totals=totals,
        missing=missing,
    )
