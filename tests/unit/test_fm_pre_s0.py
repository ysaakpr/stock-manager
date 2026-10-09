"""M17.7 pre-S0 fixes on the M17 books: corporate actions, sliced exits, upper-circuit buys.

Each test fails if the logic it guards is inverted or removed:

* a split, a bonus and a dividend in a held name are booked on both the paper broker and the
  accounting book (the share count moves, the books still reconcile), on time and learnt late; a
  late action the book cannot book mechanically escalates and trips the kill switch;
* a SELL that only participation refuses is staged as one child per session, each within the
  ceiling, journaled as one parent exit, until the book holds what the parent leaves — while the
  same size of BUY is still refused outright;
* a buy whose fill session is locked at the upper band is left unfilled and journaled
  ``UNFILLED_UPPER_CIRCUIT``; a sell on that session, or a buy on a session that trades through a
  range, fills.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from analyst.fundmanager.books import (
    CORPORATE_ACTION_EVENT,
    EXIT_COMPLETE_EVENT,
    UNFILLED_UPPER_CIRCUIT_EVENT,
    BookOrder,
    FundBook,
    PendingExit,
)
from analyst.journal.models import Decision, JournalEntry
from backtest.book_actions import BookAction, CashDividend, RescaleKind, ShareRescale
from backtest.fm_circuit import (
    BandReading,
    CircuitBasis,
    SessionPrint,
    upper_circuit_lock,
)
from backtest.fm_paper import HISTORY_SESSIONS
from dataplatform.clock import FrozenClock
from execution.broker import Side
from tests.fm_books_support import Bar, FmMarket, ListJournal, open_book, switch_at
from tests.fm_books_support import weekdays as _weekdays

SESSIONS = _weekdays(date(2025, 1, 1), 60)
D0 = SESSIONS[19]
LIQ = "INE00001A010"
THIN = "INE00002A010"
LOCKED = "INE00003A010"


class GrowingActions:
    """A corporate-action store that learns of actions over time, as the weekly refresh does."""

    def __init__(self, actions: list[BookAction] | None = None) -> None:
        self.actions: list[BookAction] = list(actions or [])

    def between(self, after: date | None, upto: date) -> tuple[BookAction, ...]:
        return tuple(
            sorted(
                (
                    a
                    for a in self.actions
                    if (after is None or a.ex_date > after) and a.ex_date <= upto
                ),
                key=lambda a: (a.ex_date, a.isin),
            )
        )


def _market(thin_value: str = "10000000") -> FmMarket:
    px = Decimal("1000")
    bars: dict[tuple[str, date], Bar] = {}
    for session in SESSIONS:
        bars[(LIQ, session)] = Bar(px, px, Decimal("10000000000"))
        bars[(THIN, session)] = Bar(px, px, Decimal(thin_value))  # ₹1 cr/day: 5% is ₹5 lakh
        bars[(LOCKED, session)] = Bar(px, px, Decimal("10000000000"))
    return FmMarket(SESSIONS, bars, {LIQ: "IT", THIN: "AUTO", LOCKED: "BANKS"})


def _step(book: FundBook, clock: FrozenClock, session: date, orders: list[BookOrder]) -> None:
    clock.freeze_at(session)
    book.execute(session)
    book.decide(session, orders)


def _events(journal: ListJournal, event: str) -> list[JournalEntry]:
    return [e for e in journal.entries if e.payload.get("event") == event]


def _bought_liq(tmp_path: Path, actions: GrowingActions, quantity: int = 50):  # type: ignore[no-untyped-def]
    clock = FrozenClock(SESSIONS[0])
    journal = ListJournal()
    switch = switch_at(tmp_path, clock)
    book, account = open_book(
        "FM-SWING-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch,
        journal=journal,
        corporate_actions=actions,
    )
    for session in SESSIONS[:19]:
        _step(book, clock, session, [])
    _step(book, clock, D0, [BookOrder(LIQ, Side.BUY, quantity, "test buy")])
    _step(book, clock, SESSIONS[20], [])  # fills at this open
    assert account.quantities() == {LIQ: quantity}
    return book, account, clock, journal, switch


# ── corporate actions ────────────────────────────────────────────────────────────────────────────


def test_a_split_in_a_held_name_is_booked_on_both_books(tmp_path: Path) -> None:
    ex = SESSIONS[22]
    split = ShareRescale(LIQ, ex, RescaleKind.SPLIT, Decimal(10), Decimal(2))  # FV 10 -> 2: x5
    book, account, clock, journal, _ = _bought_liq(tmp_path, GrowingActions([split]))
    _step(book, clock, SESSIONS[21], [])
    assert account.quantities() == {LIQ: 50}  # not before its ex-date
    clock.freeze_at(ex)
    report = book.execute(ex)
    assert account.quantities() == {LIQ: 250}
    assert report.recon is not None and report.recon.ok
    (entry,) = _events(journal, CORPORATE_ACTION_EVENT)
    assert entry.isin == LIQ and entry.decision is Decision.HOLD
    assert entry.payload["kind"] == "SPLIT" and entry.payload["late"] == "false"
    assert entry.payload["rescale"] == "10:2" and entry.payload["entitled"] == "50"
    (action,) = report.corporate_actions
    assert action.rescale == (Decimal(10), Decimal(2))
    # Booked once: the next session sees the same action and does nothing.
    book.decide(ex, [])
    _step(book, clock, SESSIONS[23], [])
    assert account.quantities() == {LIQ: 250}
    assert len(_events(journal, CORPORATE_ACTION_EVENT)) == 1


def test_a_bonus_in_a_held_name_is_booked(tmp_path: Path) -> None:
    ex = SESSIONS[22]
    bonus = ShareRescale(LIQ, ex, RescaleKind.BONUS, Decimal(3), Decimal(2))  # 1 new for 2 held
    book, account, clock, journal, _ = _bought_liq(tmp_path, GrowingActions([bonus]))
    _step(book, clock, SESSIONS[21], [])
    clock.freeze_at(ex)
    report = book.execute(ex)
    assert account.quantities() == {LIQ: 75}
    assert report.recon is not None and report.recon.ok
    (entry,) = _events(journal, CORPORATE_ACTION_EVENT)
    assert entry.payload["kind"] == "BONUS"


def test_a_dividend_on_a_held_name_is_credited_and_reconciles(tmp_path: Path) -> None:
    ex = SESSIONS[22]
    dividend = CashDividend(LIQ, ex, Decimal("12.50"))
    book, account, clock, journal, _ = _bought_liq(tmp_path, GrowingActions([dividend]))
    _step(book, clock, SESSIONS[21], [])
    before = account.cash_value
    clock.freeze_at(ex)
    report = book.execute(ex)
    assert account.cash_value - before == Decimal("625.00")
    assert report.recon is not None and report.recon.ok
    (entry,) = _events(journal, CORPORATE_ACTION_EVENT)
    assert entry.payload["cash"] == "625.00"


def test_an_action_on_a_name_never_held_books_nothing(tmp_path: Path) -> None:
    split = ShareRescale(THIN, SESSIONS[22], RescaleKind.SPLIT, Decimal(10), Decimal(1))
    book, account, clock, journal, _ = _bought_liq(tmp_path, GrowingActions([split]))
    for session in SESSIONS[21:24]:
        _step(book, clock, session, [])
    assert account.quantities() == {LIQ: 50}
    assert not _events(journal, CORPORATE_ACTION_EVENT)


def test_a_bonus_learnt_late_is_booked_on_the_session_it_is_learnt(tmp_path: Path) -> None:
    actions = GrowingActions()
    book, account, clock, journal, switch = _bought_liq(tmp_path, actions)
    for session in SESSIONS[21:25]:
        _step(book, clock, session, [])
    # The weekly refresh brings a bonus ex SESSIONS[22], which the book executed past.
    actions.actions.append(
        ShareRescale(LIQ, SESSIONS[22], RescaleKind.BONUS, Decimal(2), Decimal(1))
    )
    clock.freeze_at(SESSIONS[25])
    report = book.execute(SESSIONS[25])
    assert account.quantities() == {LIQ: 100}
    assert report.recon is not None and report.recon.ok
    (entry,) = _events(journal, CORPORATE_ACTION_EVENT)
    assert entry.payload["late"] == "true" and entry.payload["status"] == "BOOKED"
    assert not switch.is_tripped


def test_a_late_dividend_is_paid_on_the_holding_entering_its_ex_date(tmp_path: Path) -> None:
    actions = GrowingActions()
    book, account, clock, journal, _ = _bought_liq(tmp_path, actions)
    # Dividend ex D0+1 = SESSIONS[20]: the buy filled at that open, i.e. on the ex-date, so the
    # holding entering it was nothing — not entitled.
    actions.actions.append(CashDividend(LIQ, SESSIONS[20], Decimal("5")))
    # Dividend ex SESSIONS[21]: held 50 entering it.
    actions.actions.append(CashDividend(LIQ, SESSIONS[21], Decimal("2")))
    _step(book, clock, SESSIONS[21], [])
    _step(book, clock, SESSIONS[22], [])
    actions.actions.append(CashDividend(LIQ, SESSIONS[22], Decimal("3")))  # learnt late too
    before = account.cash_value
    clock.freeze_at(SESSIONS[23])
    report = book.execute(SESSIONS[23])
    # SESSIONS[23] is February's first session, so January's idle-cash interest lands too.
    assert account.cash_value - before - report.interest_credited == Decimal(50 * 3)
    assert report.recon is not None and report.recon.ok
    late = [e for e in _events(journal, CORPORATE_ACTION_EVENT) if e.payload["late"] == "true"]
    assert [e.payload["cash"] for e in late] == ["150"]


def test_a_late_split_on_a_name_traded_since_escalates_and_halts(tmp_path: Path) -> None:
    actions = GrowingActions()
    book, account, clock, journal, switch = _bought_liq(tmp_path, actions)
    _step(book, clock, SESSIONS[21], [BookOrder(LIQ, Side.BUY, 10, "add")])
    _step(book, clock, SESSIONS[22], [])  # the add fills here
    assert account.quantities() == {LIQ: 60}
    actions.actions.append(
        ShareRescale(LIQ, SESSIONS[22], RescaleKind.SPLIT, Decimal(2), Decimal(1))
    )
    clock.freeze_at(SESSIONS[23])
    book.execute(SESSIONS[23])
    assert account.quantities() == {LIQ: 60}  # not booked on a guessed entitlement
    (entry,) = _events(journal, CORPORATE_ACTION_EVENT)
    assert entry.decision is Decision.ESCALATE and entry.payload["status"] == "ESCALATED"
    assert switch.is_tripped


def test_the_corporate_action_ledger_persists_with_the_account(tmp_path: Path) -> None:
    from backtest.book_actions import BookActionCalendar
    from backtest.cash_interest import load_repo_rate_schedule
    from backtest.fm_circuit import NoCircuitData
    from backtest.fm_paper import M17PaperAccount

    split = ShareRescale(LIQ, SESSIONS[22], RescaleKind.SPLIT, Decimal(10), Decimal(2))
    actions = GrowingActions([split])
    book, account, clock, _, switch = _bought_liq(tmp_path, actions)
    _step(book, clock, SESSIONS[21], [])
    _step(book, clock, SESSIONS[22], [])
    document = account.to_document()
    restored = M17PaperAccount.restore(
        document,
        account_id=account.account_id,
        market=_market(),
        kill_switch=switch,
        clock=clock,
        schedule=load_repo_rate_schedule(),
        book_digest=account.book_digest,
        corporate_actions=actions,
        circuit=NoCircuitData(),
    )
    clock.freeze_at(SESSIONS[23])
    restored.execute_session(SESSIONS[23])
    assert restored.quantities() == {LIQ: 250}  # the split is not booked a second time
    stale = {k: v for k, v in document.items() if k != "corporate_actions"}
    try:
        M17PaperAccount.restore(
            stale,
            account_id=account.account_id,
            market=_market(),
            kill_switch=switch,
            clock=clock,
            schedule=load_repo_rate_schedule(),
            book_digest=account.book_digest,
            corporate_actions=BookActionCalendar(),
            circuit=NoCircuitData(),
        )
    except ValueError as exc:
        assert "corporate-action ledger" in str(exc)
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("a pre-M17.7 document was restored with an empty ledger")
    assert HISTORY_SESSIONS >= 20


# ── sliced exits ─────────────────────────────────────────────────────────────────────────────────


def _holding_thin(tmp_path: Path, quantity: int):  # type: ignore[no-untyped-def]
    """A 1 cr book that bought ``quantity`` THIN when it was liquid; ₹1 cr/day from SESSIONS[21]."""
    clock = FrozenClock(SESSIONS[0])
    journal = ListJournal()
    px = Decimal("1000")
    bars: dict[tuple[str, date], Bar] = {}
    for i, session in enumerate(SESSIONS):
        bars[(LIQ, session)] = Bar(px, px, Decimal("10000000000"))
        # Liquid through D0 (the buy clears), ₹1 cr/day from 20 sessions before the sells.
        value = Decimal("10000000000") if i <= 20 else Decimal("10000000")
        bars[(THIN, session)] = Bar(px, px, value)
    market = FmMarket(SESSIONS, bars, {LIQ: "IT", THIN: "AUTO"})
    book, account = open_book(
        "FM-SWING-1CR",
        market=market,
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    _step(book, clock, D0, [BookOrder(THIN, Side.BUY, quantity, "test buy")])
    _step(book, clock, SESSIONS[20], [])
    assert account.quantities() == {THIN: quantity}
    return book, account, clock, journal


def test_an_over_participation_sell_is_sliced_across_sessions_until_filled(
    tmp_path: Path,
) -> None:
    book, account, clock, journal = _holding_thin(tmp_path, 900)
    start = 41  # 20 sessions of ₹1 cr/day behind it: the ceiling is ₹5 lakh = 500 shares
    clock.freeze_at(SESSIONS[start])
    book.execute(SESSIONS[start])
    report = book.decide(SESSIONS[start], [BookOrder(THIN, Side.SELL, 900, "exit thin")])
    assert not [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]
    assert len(report.staged) == 1
    first = [e for e in journal.entries if e.decision is Decision.SELL]
    assert [e.payload["quantity"] for e in first] == ["500"]
    parent = first[0].payload["order_uid"]
    assert first[0].payload["exit_parent"] == parent
    assert first[0].payload["exit_parent_quantity"] == "900"
    assert book.pending_exits[THIN].floor == 0

    held = []
    for session in SESSIONS[start + 1 : start + 5]:
        _step(book, clock, session, [])
        held.append(account.quantities().get(THIN, 0))
    sells = [e for e in journal.entries if e.decision is Decision.SELL]
    # One child per session, each within the ceiling, all naming one parent.
    assert [e.payload["quantity"] for e in sells] == ["500", "400"]
    assert {e.payload["exit_parent"] for e in sells} == {parent}
    assert len({e.trading_date for e in sells}) == 2
    assert all(Decimal(e.payload["notional"]) <= Decimal("500000") for e in sells)
    assert held == [400, 0, 0, 0]
    assert account.quantities() == {}
    assert THIN not in book.pending_exits
    (done,) = _events(journal, EXIT_COMPLETE_EVENT)
    assert done.payload["exit_parent"] == parent and done.payload["exit_children"] == "2"


def test_a_partial_trim_leaves_its_floor(tmp_path: Path) -> None:
    book, account, clock, journal = _holding_thin(tmp_path, 900)
    _step(book, clock, SESSIONS[41], [BookOrder(THIN, Side.SELL, 700, "trim thin")])
    for session in SESSIONS[42:45]:
        _step(book, clock, session, [])
    assert account.quantities() == {THIN: 200}
    sells = [e for e in journal.entries if e.decision is Decision.SELL]
    assert [e.payload["quantity"] for e in sells] == ["500", "200"]


def test_an_over_participation_buy_is_still_refused_outright(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-1CR",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    _step(book, clock, D0, [BookOrder(THIN, Side.BUY, 600, "too big a buy")])
    (block,) = [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]
    assert block.payload["rails"] == "PARTICIPATION"
    assert not [e for e in journal.entries if e.decision is Decision.BUY]
    assert not book.pending_exits


def test_a_sell_another_rail_refuses_is_not_sliced(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-1CR",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    _step(book, clock, D0, [BookOrder(THIN, Side.BUY, 400, "buy")])
    # Inside the minimum hold and over participation: refused whole, never sliced past MIN_HOLD.
    _step(book, clock, SESSIONS[20], [BookOrder(THIN, Side.SELL, 400, "sell")])
    _step(book, clock, SESSIONS[21], [BookOrder(THIN, Side.SELL, 400, "sell")])
    blocks = [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]
    assert blocks and all("MIN_HOLD" in b.payload["rails"] for b in blocks)
    assert not [e for e in journal.entries if e.decision is Decision.SELL]
    assert not book.pending_exits


def test_a_new_decision_on_the_name_replaces_the_open_exit(tmp_path: Path) -> None:
    book, account, clock, journal = _holding_thin(tmp_path, 900)
    _step(book, clock, SESSIONS[41], [BookOrder(THIN, Side.SELL, 900, "exit thin")])
    _step(book, clock, SESSIONS[42], [BookOrder(THIN, Side.SELL, 100, "smaller trim instead")])
    sells = [e for e in journal.entries if e.decision is Decision.SELL]
    assert [e.payload["quantity"] for e in sells] == ["500", "100"]
    assert "exit_parent" not in sells[1].payload
    assert THIN not in book.pending_exits
    clock.freeze_at(SESSIONS[43])
    book.execute(SESSIONS[43])
    assert account.quantities() == {THIN: 300}


def test_pending_exits_round_trip_their_document() -> None:
    pending = PendingExit(THIN, "m17_x:2025-01-01:000", D0, 1200, 0, "exit", "STAGED", 2)
    book_document = [pending.to_document()]
    assert FundBook.pending_exits_from(book_document) == {THIN: pending}


# ── upper-circuit buys ───────────────────────────────────────────────────────────────────────────


class LockedOn:
    """A `CircuitMarket` where ``LOCKED`` opens and stays at +5% on ``session``; band 5% unless
    ``band`` says otherwise."""

    def __init__(self, session: date, band: BandReading | None = None) -> None:
        self.session = session
        self.band = BandReading(known=True, band_pct=Decimal(5)) if band is None else band

    def session_print(self, isin: str, session: date) -> SessionPrint | None:
        if isin == LOCKED and session == self.session:
            px = Decimal("1050.00")
            return SessionPrint(isin, session, px, px, px, Decimal("1000"))
        return SessionPrint(
            isin, session, Decimal(1000), Decimal(1010), Decimal(990), Decimal(1000)
        )

    def price_band(self, isin: str, session: date) -> BandReading:
        return self.band


def test_a_buy_into_an_upper_circuit_lock_is_left_unfilled(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, account = open_book(
        "FM-SWING-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
        circuit=LockedOn(SESSIONS[20]),
    )
    _step(
        book,
        clock,
        D0,
        [BookOrder(LOCKED, Side.BUY, 10, "buy"), BookOrder(LIQ, Side.BUY, 10, "buy")],
    )
    clock.freeze_at(SESSIONS[20])
    report = book.execute(SESSIONS[20])
    assert [f.isin for f in report.fills] == [LIQ]
    assert account.quantities() == {LIQ: 10}
    assert report.recon is not None and report.recon.ok
    (entry,) = _events(journal, UNFILLED_UPPER_CIRCUIT_EVENT)
    assert entry.isin == LOCKED and entry.payload["basis"] == "BAND"
    assert entry.payload["quantity"] == "10"


def test_a_buy_on_the_session_after_the_lock_fills(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, account = open_book(
        "FM-SWING-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
        circuit=LockedOn(SESSIONS[20]),
    )
    _step(book, clock, D0, [])
    _step(book, clock, SESSIONS[20], [BookOrder(LOCKED, Side.BUY, 10, "buy")])
    clock.freeze_at(SESSIONS[21])
    book.execute(SESSIONS[21])
    assert account.quantities() == {LOCKED: 10}
    assert not _events(journal, UNFILLED_UPPER_CIRCUIT_EVENT)


def _print(open_: str, high: str, low: str, prev: str = "100") -> SessionPrint:
    return SessionPrint(LOCKED, D0, Decimal(open_), Decimal(high), Decimal(low), Decimal(prev))


def test_the_lock_test_needs_open_high_and_low_at_the_band() -> None:
    five = BandReading(known=True, band_pct=Decimal(5))
    locked = upper_circuit_lock(_print("105", "105", "105"), five)
    assert locked is not None and locked.locked and locked.basis is CircuitBasis.BAND
    # Rounded to the tick below the exact band figure: still at the band.
    tick = upper_circuit_lock(_print("104.95", "104.95", "104.95"), five)
    assert tick is not None and tick.locked
    # Traded through a range, or flat below the band, or locked at the lower band: not locked.
    assert upper_circuit_lock(_print("105", "105", "104"), five) is None
    below = upper_circuit_lock(_print("103", "103", "103"), five)
    assert below is not None and not below.locked
    lower = upper_circuit_lock(_print("95", "95", "95"), five)
    assert lower is not None and not lower.locked
    # "No Band" (F&O) is never locked by a static band.
    assert upper_circuit_lock(_print("105", "105", "105"), BandReading(True, None)) is None


def test_with_no_band_known_the_smallest_band_stands_in() -> None:
    missing = BandReading(known=False)
    two = upper_circuit_lock(_print("102", "102", "102"), missing)
    assert two is not None and two.locked and two.basis is CircuitBasis.SMALLEST_BAND
    under = upper_circuit_lock(_print("101.9", "101.9", "101.9"), missing)
    assert under is not None and not under.locked
