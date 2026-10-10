"""M17.5 — multi-book paper execution: per-manager books, the participation rail, the M17 rails.

Each test here would pass against a wrong implementation only if it were itself wrong:

* the rails come from the roster, never from a second copy of its numbers;
* an oversized order in a thin name is refused by the participation rail, journaled ``RAIL_BLOCK``
  with ``payload.rails = PARTICIPATION`` in the book's own stream, and never reaches the broker —
  while the same order in a liquid name is staged and fills at the next open;
* the participation median is the decision session's (a later spike does not loosen it, a market
  that answers with a later session is refused);
* minimum hold, no short, no F&O and no margin each refuse what they exist to refuse;
* two books never share a position, a rupee or a journal stream, and one kill switch halts both;
* idle cash accrues repo - 50 bp through the existing schedule and the books still reconcile;
* the account persists in the M15.3 ``paper_session`` shape and continues byte-for-byte;
* ``execution.kite_broker`` is unreachable from M17, by the import graph and in a fresh process.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from collections import deque
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from analyst.fundmanager import load_roster
from analyst.fundmanager.books import (
    BOOK_HALTED_EVENT,
    M17_KILL_SWITCH_ACCOUNT,
    RECON_BREAK_EVENT,
    RECON_EVENT,
    BookError,
    BookOrder,
    FundBook,
    FundDesk,
    FutureDataError,
    book_rails,
    median_traded_value,
    paper_account_id,
    tradable_mandates,
)
from analyst.journal.models import Actor, Decision, JournalEntry
from analyst.rails import BookOrderFacts, Lot, Portfolio, ProposedOrder, RailId, check_book_order
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.cash_interest import CashInterestAccrual, load_repo_rate_schedule
from backtest.fm_circuit import NoCircuitData
from backtest.fm_paper import CorporateActionLedger, M17PaperAccount, _AccountBook
from backtest.paper_session import (
    InMemoryPaperSessionStore,
    PaperModeViolationError,
    PaperSessionRecord,
    SessionOutcome,
)
from dataplatform.clock import FrozenClock
from execution.broker import OrderRequest, Side
from execution.costs import CostModel, load_rate_card
from execution.kill_switch import TripSource
from execution.recon import RecordingAlerter
from execution.sim_broker import SimBroker
from tests.fm_books_support import Bar, FmMarket, ListJournal, open_book, roster_rails, switch_at
from tests.fm_books_support import weekdays as _weekdays

REPO = Path(__file__).resolve().parents[2]

SESSIONS = _weekdays(date(2025, 1, 1), 45)
#: The first session with a full 20-session participation lookback behind it.
D0 = SESSIONS[19]


def isin(n: int) -> str:
    return f"INE{n:05d}A010"


LIQ, THIN, SPIKE, BE_NAME, OTHER = isin(1), isin(2), isin(3), isin(4), isin(5)


def _market(
    *,
    price: str = "1000",
    thin_value: str = "10000000",  # ₹1 cr/day: 5% of it is ₹5 lakh
    leak_future: bool = False,
) -> FmMarket:
    bars: dict[tuple[str, date], Bar] = {}
    px = Decimal(price)
    for i, session in enumerate(SESSIONS):
        bars[(LIQ, session)] = Bar(px, px, Decimal("10000000000"))  # ₹1,000 cr/day
        bars[(OTHER, session)] = Bar(px, px, Decimal("10000000000"))
        bars[(THIN, session)] = Bar(px, px, Decimal(thin_value))
        # A thin name whose turnover jumps a hundredfold *after* D0: the decision must not see it.
        spike = Decimal("10000000") if session <= D0 else Decimal("1000000000")
        bars[(SPIKE, session)] = Bar(px, px, spike)
        bars[(BE_NAME, session)] = Bar(px, px, Decimal("10000000000"), series="BE")
        del i
    sectors = {LIQ: "IT", OTHER: "BANKS", THIN: "IT", SPIKE: "AUTO", BE_NAME: "METALS"}
    return FmMarket(SESSIONS, bars, sectors, leak_future=leak_future)


def _step(
    book: FundBook, clock: FrozenClock, session: date, orders: list[BookOrder]
) -> tuple[object, object]:
    clock.freeze_at(session)
    return book.execute(session), book.decide(session, orders)


def _buy(name: str, quantity: int) -> BookOrder:
    return BookOrder(name, Side.BUY, quantity, f"test buy of {name}")


def _sell(name: str, quantity: int) -> BookOrder:
    return BookOrder(name, Side.SELL, quantity, f"test sell of {name}")


def _blocks(journal: ListJournal) -> list[JournalEntry]:
    return [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]


# ── the rails come from the roster ───────────────────────────────────────────────────────────────


def test_every_tradable_book_takes_its_rails_from_the_roster() -> None:
    roster = load_roster()
    mandates = tradable_mandates(roster)
    assert {m.id for m in mandates} == {
        m.id for m in (*roster.manager_books, *roster.controls, *roster.styles)
    }
    for mandate in mandates:
        rails = book_rails(mandate, roster.rails)
        assert rails.max_position_pct == mandate.max_position_pct
        assert rails.max_sector_pct == mandate.max_sector_pct
        assert rails.max_positions == mandate.max_positions
        assert rails.participation_max_pct == roster.rails.participation_max_pct
        assert rails.participation_lookback_sessions == roster.rails.participation_lookback_sessions
        assert rails.min_hold_sessions == roster.rails.min_hold_sessions
        assert rails.equity_series == frozenset({mandate.universe.series})


def test_paper_account_ids_are_distinct_paper_session_book_ids() -> None:
    ids = [paper_account_id(m.id) for m in tradable_mandates(load_roster())]
    assert len(set(ids)) == len(ids)
    assert paper_account_id("FM-SWING-BRK-10L") == "m17_fm_swing_brk_10l"
    for account in ids:
        # The paper_session.book_id CHECK and the kill-switch account shape (0012_paper_session).
        assert account[0].isalpha() and account == account.lower() and len(account) <= 64


def test_median_is_exact() -> None:
    assert median_traded_value([Decimal(3), Decimal(1), Decimal(2)]) == Decimal(2)
    assert median_traded_value([Decimal(4), Decimal(1), Decimal(2), Decimal(3)]) == Decimal("2.5")


# ── acceptance 1: the participation rail ─────────────────────────────────────────────────────────


def test_an_oversized_order_in_a_thin_name_is_refused_by_participation_and_journaled(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    switch = switch_at(tmp_path, clock)
    book, account = open_book(
        "FM-SWING-BRK-1CR", market=_market(), clock=clock, kill_switch=switch, journal=journal
    )
    # ₹6 lakh each: 6% of a ₹1 cr book (inside the 10% position cap), but 6% of THIN's ₹1 cr/day
    # median against the 5% participation cap. The liquid name trades ₹1,000 cr/day.
    _, report = _step(book, clock, D0, [_buy(THIN, 600), _buy(LIQ, 600)])

    blocks = _blocks(journal)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.isin == THIN
    assert block.actor is Actor.RAILS
    assert block.case_id == "FM-SWING-BRK-1CR"
    assert block.payload["rails"] == RailId.PARTICIPATION.value
    assert "PARTICIPATION" in (block.rationale or "")
    assert block.payload["book"] == "FM-SWING-BRK-1CR"
    assert [order.isin for order, _ in report.staged] == [LIQ]  # type: ignore[attr-defined]

    # Only the liquid order reached the broker, and it fills at the next session's open.
    next_session = SESSIONS[20]
    clock.freeze_at(next_session)
    executed = book.execute(next_session)
    assert [(f.isin, f.quantity) for f in executed.fills] == [(LIQ, 600)]
    assert account.quantities() == {LIQ: 600}
    assert executed.recon is not None and executed.recon.ok


def test_the_participation_ceiling_is_exact_at_the_boundary(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-BRK-1CR",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    # 500 x ₹1,000 = ₹5 lakh = exactly 5% of ₹1 cr: allowed. One share more is refused.
    _, report = _step(book, clock, D0, [_buy(THIN, 500)])
    assert not _blocks(journal)
    assert len(report.staged) == 1  # type: ignore[attr-defined]
    _, report = _step(book, clock, SESSIONS[20], [_buy(OTHER, 1), _buy(SPIKE, 501)])
    assert [b.payload["rails"] for b in _blocks(journal)] == ["PARTICIPATION"]


def test_the_median_is_the_decision_sessions_not_a_later_one(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-BRK-1CR",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    # SPIKE's turnover goes from ₹1 cr to ₹100 cr/day after D0; at D0 the cap is still ₹5 lakh.
    _step(book, clock, D0, [_buy(SPIKE, 600)])
    assert [b.payload["rails"] for b in _blocks(journal)] == ["PARTICIPATION"]


def test_a_market_that_answers_with_a_later_session_is_refused(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    book, _ = open_book(
        "FM-SWING-BRK-1CR",
        market=_market(leak_future=True),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=ListJournal(),
    )
    clock.freeze_at(D0)
    book.execute(D0)
    with pytest.raises(FutureDataError, match="never reads ahead"):
        book.decide(D0, [_buy(SPIKE, 10)])


def test_a_name_without_a_full_lookback_is_refused(tmp_path: Path) -> None:
    clock = FrozenClock(SESSIONS[18])
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-BRK-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    _step(book, clock, SESSIONS[18], [_buy(LIQ, 1)])  # 19 sessions of history, 20 required
    (block,) = _blocks(journal)
    assert block.payload["rails"] == "PARTICIPATION"
    assert "19 session(s) knowable" in (block.rationale or "")


# ── minimum hold, no short, no F&O, no margin ────────────────────────────────────────────────────


def test_a_sell_inside_the_minimum_hold_is_refused_and_one_after_it_fills(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, account = open_book(
        "FM-SWING-BRK-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    min_hold = load_roster().rails.min_hold_sessions
    _step(book, clock, D0, [_buy(LIQ, 50)])
    fill_session = SESSIONS[20]  # the buy fills at this open
    # Decisions 0 .. min_hold-1 sessions after the fill are refused by MIN_HOLD.
    for offset in range(min_hold):
        _step(book, clock, SESSIONS[20 + offset], [_sell(LIQ, 50)])
    assert [b.payload["rails"] for b in _blocks(journal)] == ["MIN_HOLD"] * min_hold
    assert book.last_buy_fill == {LIQ: fill_session}
    # min_hold sessions after the fill the sell is allowed, and fills at the next open.
    _, report = _step(book, clock, SESSIONS[20 + min_hold], [_sell(LIQ, 50)])
    assert len(report.staged) == 1  # type: ignore[attr-defined]
    clock.freeze_at(SESSIONS[21 + min_hold])
    executed = book.execute(SESSIONS[21 + min_hold])
    assert [(f.isin, f.side) for f in executed.fills] == [(LIQ, Side.SELL)]
    assert account.quantities() == {}


def test_a_short_sale_is_refused(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-BRK-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    _step(book, clock, D0, [_sell(LIQ, 10)])
    (block,) = _blocks(journal)
    assert block.payload["rails"].split(",")[0] == "NO_SHORT"


def test_an_oversell_of_a_held_name_is_refused_as_a_short(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-BRK-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    _step(book, clock, D0, [_buy(LIQ, 50)])
    for session in SESSIONS[20:23]:
        _step(book, clock, session, [])
    _step(book, clock, SESSIONS[23], [_sell(LIQ, 51)])
    assert [b.payload["rails"] for b in _blocks(journal)] == ["NO_SHORT"]


def test_a_non_equity_series_is_refused_as_fno(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    journal = ListJournal()
    book, _ = open_book(
        "FM-SWING-BRK-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    _step(book, clock, D0, [_buy(BE_NAME, 10)])
    assert [b.payload["rails"] for b in _blocks(journal)] == ["NO_FNO"]


def _facts(**overrides: object) -> BookOrderFacts:
    values: dict[str, object] = {
        "decision_session": D0,
        "series": "EQ",
        "median_traded_value": Decimal("10000000000"),
        "median_sessions": 20,
        "sessions_since_buy_fill": None,
        "spendable_cash": Decimal("1000000"),
    }
    values.update(overrides)
    return BookOrderFacts(**values)  # type: ignore[arg-type]


def _proposed(
    name: str, side: Side, quantity: int, price: str = "100", sector: str = "IT"
) -> ProposedOrder:
    return ProposedOrder(OrderRequest(name, side, quantity), Decimal(price), sector)


def test_a_buy_beyond_spendable_cash_is_refused_as_margin() -> None:
    rails, _ = roster_rails("FM-SWING-BRK-10L")
    # ₹1,00,000 book: ₹99,000 deployed across 11 names, ₹1,000 cash. A ₹5,000 buy (5%) is inside
    # every cap but needs cash the book does not have.
    lots = tuple(Lot(isin(10 + i), f"S{i}", 90, Decimal("100")) for i in range(11))
    book = Portfolio("FM-SWING-BRK-10L", lots, Decimal("1000"))
    verdict = check_book_order(
        _proposed(isin(40), Side.BUY, 50), book, rails, _facts(spendable_cash=Decimal("1000"))
    )
    assert verdict.breached_rails == (RailId.NO_MARGIN,)
    ok = check_book_order(
        _proposed(isin(40), Side.BUY, 10), book, rails, _facts(spendable_cash=Decimal("1000"))
    )
    assert ok.allowed


def test_position_sector_and_name_caps_bind_a_buy() -> None:
    rails, _ = roster_rails("FM-SWING-BRK-10L")
    empty = Portfolio("FM-SWING-BRK-10L", (), Decimal("1000000"))
    over_position = check_book_order(_proposed(LIQ, Side.BUY, 1001), empty, rails, _facts())
    assert over_position.breached_rails == (RailId.MAX_POSITION,)
    at_position = check_book_order(_proposed(LIQ, Side.BUY, 1000), empty, rails, _facts())
    assert at_position.allowed

    three_it = tuple(Lot(isin(20 + i), "IT", 1000, Decimal("100")) for i in range(3))  # 30% IT
    sector_full = Portfolio("FM-SWING-BRK-10L", three_it, Decimal("700000"))
    assert check_book_order(
        _proposed(LIQ, Side.BUY, 1, sector="IT"), sector_full, rails, _facts()
    ).breached_rails == (RailId.MAX_SECTOR,)
    assert check_book_order(
        _proposed(LIQ, Side.BUY, 1, sector="BANKS"), sector_full, rails, _facts()
    ).allowed

    full = tuple(Lot(isin(50 + i), f"S{i % 5}", 10, Decimal("100")) for i in range(15))
    names_full = Portfolio("FM-SWING-BRK-10L", full, Decimal("985000"))
    assert check_book_order(
        _proposed(LIQ, Side.BUY, 1, sector="NEW"), names_full, rails, _facts()
    ).breached_rails == (RailId.MAX_POSITIONS,)
    # Adding to a held name is not a new name.
    assert check_book_order(
        _proposed(isin(50), Side.BUY, 1, sector="S0"), names_full, rails, _facts()
    ).allowed


def test_a_sell_never_trips_a_buy_cap_and_every_breach_is_reported() -> None:
    rails, _ = roster_rails("FM-SWING-BRK-10L")
    lot = Lot(LIQ, "IT", 5000, Decimal("100"))  # 50% of the book, far over every cap
    book = Portfolio("FM-SWING-BRK-10L", (lot,), Decimal("500000"))
    assert check_book_order(
        _proposed(LIQ, Side.SELL, 10), book, rails, _facts(sessions_since_buy_fill=5)
    ).allowed
    everything = check_book_order(
        _proposed(THIN, Side.BUY, 20000),
        book,
        rails,
        _facts(series="FUT", median_traded_value=None, median_sessions=3),
    )
    assert set(everything.breached_rails) == {
        RailId.NO_FNO,
        RailId.NO_MARGIN,
        RailId.PARTICIPATION,
        RailId.MAX_POSITION,
        RailId.MAX_SECTOR,
    }


# ── acceptance 3: two books share nothing ────────────────────────────────────────────────────────


def _two_books(tmp_path: Path) -> tuple[FundDesk, FundBook, FundBook, FrozenClock, ListJournal]:
    clock = FrozenClock(D0)
    journal = ListJournal()  # one physical journal, as in production: streams are per book id
    switch = switch_at(tmp_path, clock)
    market = _market()
    manager, _ = open_book(
        "FM-SWING-BRK-10L", market=market, clock=clock, kill_switch=switch, journal=journal
    )
    control, _ = open_book(
        "CTRL-FM-SWING-BRK-10L", market=market, clock=clock, kill_switch=switch, journal=journal
    )
    return FundDesk([manager, control], kill_switch=switch), manager, control, clock, journal


def test_two_books_never_share_a_position_cash_or_journal_stream(tmp_path: Path) -> None:
    desk, manager, control, clock, journal = _two_books(tmp_path)
    clock.freeze_at(D0)
    desk.execute(D0)
    desk.decide(
        D0,
        {
            "FM-SWING-BRK-10L": [_buy(LIQ, 50), _buy(THIN, 60)],  # 5% and 6% of a 10L book
            "CTRL-FM-SWING-BRK-10L": [_buy(OTHER, 70)],
        },
    )
    clock.freeze_at(SESSIONS[20])
    reports = desk.execute(SESSIONS[20])

    assert manager.account.quantities() == {LIQ: 50, THIN: 60}
    assert control.account.quantities() == {OTHER: 70}
    assert manager.account.cash_value != control.account.cash_value
    capital = Decimal("1000000")
    spent_manager = sum((f.cost.net_amount for f in reports["FM-SWING-BRK-10L"].fills), Decimal(0))
    spent_control = sum(
        (f.cost.net_amount for f in reports["CTRL-FM-SWING-BRK-10L"].fills), Decimal(0)
    )
    assert manager.account.cash_value == capital - spent_manager
    assert control.account.cash_value == capital - spent_control

    streams: dict[str, list[JournalEntry]] = {}
    for entry in journal.entries:
        assert entry.case_id is not None
        assert entry.payload["book"] == entry.case_id
        streams.setdefault(entry.case_id, []).append(entry)
    assert set(streams) == {"FM-SWING-BRK-10L", "CTRL-FM-SWING-BRK-10L"}
    traded = {
        book_id: {e.isin for e in entries if e.decision in {Decision.BUY, Decision.SELL}}
        for book_id, entries in streams.items()
    }
    assert traded == {"FM-SWING-BRK-10L": {LIQ, THIN}, "CTRL-FM-SWING-BRK-10L": {OTHER}}
    # Each book's reconciliation reads its own account: its own cash and positions only.
    recon = {
        e.case_id: e.payload
        for e in journal.entries
        if e.payload.get("event") == RECON_EVENT and e.trading_date == SESSIONS[20]
    }
    assert recon["FM-SWING-BRK-10L"]["positions"] == "2"
    assert recon["CTRL-FM-SWING-BRK-10L"]["positions"] == "1"


def test_the_desk_refuses_books_that_share_an_id_an_account_or_a_switch(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    switch = switch_at(tmp_path, clock)
    market = _market()
    journal = ListJournal()
    book, account = open_book(
        "FM-SWING-BRK-10L", market=market, clock=clock, kill_switch=switch, journal=journal
    )
    rails, _ = roster_rails("CTRL-FM-SWING-BRK-10L")
    shares_account = FundBook(
        book_id="CTRL-FM-SWING-BRK-10L",
        rails=rails,
        account=account,
        market=market,
        journal=journal,
        kill_switch=switch,
        clock=clock,
    )
    with pytest.raises(BookError, match="share one paper account"):
        FundDesk([book, shares_account], kill_switch=switch)
    twin, _ = open_book(
        "FM-SWING-BRK-10L", market=market, clock=clock, kill_switch=switch, journal=journal
    )
    with pytest.raises(BookError, match="share an id"):
        FundDesk([book, twin], kill_switch=switch)
    other_switch = switch_at(tmp_path / "elsewhere", clock)
    stray, _ = open_book(
        "CTRL-FM-SWING-BRK-10L",
        market=market,
        clock=clock,
        kill_switch=other_switch,
        journal=journal,
    )
    with pytest.raises(BookError, match="one M17 kill switch"):
        FundDesk([book, stray], kill_switch=switch)


def test_a_book_stamps_its_own_id_on_whatever_it_journals(tmp_path: Path) -> None:
    _, manager, _, clock, journal = _two_books(tmp_path)
    clock.freeze_at(D0)
    manager.execute(D0)
    manager.decide(D0, [])
    assert {e.case_id for e in journal.entries} == {"FM-SWING-BRK-10L"}
    assert [e.decision for e in journal.entries] == [Decision.HEARTBEAT, Decision.HOLD]


# ── one kill switch stops every M17 book ─────────────────────────────────────────────────────────


def test_one_switch_halts_every_book_and_lapses_their_orders(tmp_path: Path) -> None:
    desk, manager, control, clock, journal = _two_books(tmp_path)
    clock.freeze_at(D0)
    desk.execute(D0)
    desk.decide(
        D0, {"FM-SWING-BRK-10L": [_buy(LIQ, 10)], "CTRL-FM-SWING-BRK-10L": [_buy(OTHER, 10)]}
    )
    assert manager.kill_switch.state.tripped is False
    manager.kill_switch.trip(reason="owner halt drill", source=TripSource.MANUAL)

    clock.freeze_at(SESSIONS[20])
    executed = desk.execute(SESSIONS[20])
    decided = desk.decide(SESSIONS[20], {"FM-SWING-BRK-10L": [_buy(THIN, 10)]})
    for book_id in ("FM-SWING-BRK-10L", "CTRL-FM-SWING-BRK-10L"):
        assert executed[book_id].halted and executed[book_id].fills == ()
        assert len(executed[book_id].lapsed) == 1
        assert decided[book_id].halted and decided[book_id].staged == ()
    assert manager.account.quantities() == {} and control.account.quantities() == {}
    halts = [e for e in journal.entries if e.payload.get("event") == BOOK_HALTED_EVENT]
    assert {(e.case_id, e.payload["step"]) for e in halts} == {
        ("FM-SWING-BRK-10L", "filled"),
        ("CTRL-FM-SWING-BRK-10L", "filled"),
        ("FM-SWING-BRK-10L", "staged"),
        ("CTRL-FM-SWING-BRK-10L", "staged"),
    }
    assert all(e.decision is Decision.SKIPPED_DATA_RED for e in halts)
    assert manager.kill_switch.state.source is TripSource.MANUAL
    assert str(manager.kill_switch._path).endswith(f"{M17_KILL_SWITCH_ACCOUNT}.json")


def test_a_recon_break_in_one_book_halts_the_other(tmp_path: Path) -> None:
    desk, manager, control, clock, journal = _two_books(tmp_path)
    clock.freeze_at(D0)
    desk.execute(D0)
    desk.decide(
        D0, {"FM-SWING-BRK-10L": [_buy(LIQ, 10)], "CTRL-FM-SWING-BRK-10L": [_buy(OTHER, 10)]}
    )
    # Corrupt the manager's accounting book behind the broker's back: a phantom rupee.
    account = manager.account
    assert isinstance(account, M17PaperAccount)
    account._book.book.deposit(D0, Decimal("1"))
    clock.freeze_at(SESSIONS[20])
    executed = desk.execute(SESSIONS[20])
    # Both books' fills were due and happened; the manager's reconciliation broke and tripped.
    assert (
        executed["FM-SWING-BRK-10L"].recon is not None and not executed["FM-SWING-BRK-10L"].recon.ok
    )
    assert (
        executed["CTRL-FM-SWING-BRK-10L"].recon is not None
        and executed["CTRL-FM-SWING-BRK-10L"].recon.ok
    )
    assert manager.kill_switch.is_tripped
    (escalation,) = [e for e in journal.entries if e.payload.get("event") == RECON_BREAK_EVENT]
    assert escalation.case_id == "FM-SWING-BRK-10L" and escalation.decision is Decision.ESCALATE
    decided = desk.decide(SESSIONS[20], {"CTRL-FM-SWING-BRK-10L": [_buy(THIN, 10)]})
    assert decided["CTRL-FM-SWING-BRK-10L"].halted and decided["CTRL-FM-SWING-BRK-10L"].staged == ()
    assert control.account.quantities() == {OTHER: 10}


# ── idle cash interest ───────────────────────────────────────────────────────────────────────────


def test_idle_cash_accrues_repo_less_50bp_and_the_book_still_reconciles(tmp_path: Path) -> None:
    clock = FrozenClock(SESSIONS[0])
    journal = ListJournal()
    book, account = open_book(
        "FM-SWING-BRK-10L",
        market=_market(),
        clock=clock,
        kill_switch=switch_at(tmp_path, clock),
        journal=journal,
    )
    schedule = load_repo_rate_schedule()
    january = [s for s in SESSIONS if s.month == 1]
    for session in january:
        _step(book, clock, session, [])
    first_february = next(s for s in SESSIONS if s.month == 2)
    clock.freeze_at(first_february)
    report = book.execute(first_february)

    capital = Decimal("1000000")
    expected = Decimal(0)
    day = january[0]
    while day < date(2025, 2, 1):
        expected += capital * (schedule.repo_rate(day) - Decimal("0.0050")) / Decimal(365)
        day += timedelta(days=1)
    expected = expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert expected > 0
    assert report.interest_credited == expected
    assert account.cash_value == capital + expected
    assert report.recon is not None and report.recon.ok
    heartbeat = journal.entries[-1]
    assert heartbeat.payload["interest_credited"] == str(expected)


# ── persistence on the M15.3 paper-session ledger ────────────────────────────────────────────────


def test_an_account_persists_as_a_paper_session_row_and_continues_exactly(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    switch = switch_at(tmp_path, clock)
    market = _market()
    book, account = open_book(
        "FM-SWING-BRK-10L", market=market, clock=clock, kill_switch=switch, journal=ListJournal()
    )
    _step(book, clock, D0, [_buy(LIQ, 50)])
    _step(book, clock, SESSIONS[20], [_buy(OTHER, 40)])  # staged, unfilled at persistence time

    document = {**account.to_document(), "buy_fills": book.buy_fills_document()}
    store = InMemoryPaperSessionStore()
    record = PaperSessionRecord(
        book_id=account.account_id,
        trading_date=SESSIONS[20],
        outcome=SessionOutcome.COMPLETED,
        reason="m17 book session",
        rebalanced=False,
        journal_digest="0" * 64,
        book_state=document,
        book_digest=account.book_digest,
        expected_book=document["expected_book"],
    )
    store.record(record, recorded_at=clock.now())
    stored = store.latest_completed(account.account_id, before=SESSIONS[21])
    assert stored is not None and stored.book_state is not None
    assert stored.broker_state().to_document() == document["broker"]

    restored = M17PaperAccount.restore(
        stored.book_state,
        account_id=account.account_id,
        market=market,
        kill_switch=switch,
        clock=clock,
        schedule=load_repo_rate_schedule(),
        corporate_actions=BookActionCalendar(),
        circuit=NoCircuitData(),
        book_digest=stored.book_digest or "",
        alerter=RecordingAlerter(),
    )
    rails, _ = roster_rails("FM-SWING-BRK-10L")
    again = FundBook(
        book_id="FM-SWING-BRK-10L",
        rails=rails,
        account=restored,
        market=market,
        journal=ListJournal(),
        kill_switch=switch,
        clock=clock,
        last_buy_fill=FundBook.buy_fills_from(stored.book_state["buy_fills"]),
    )
    clock.freeze_at(SESSIONS[21])
    live = book.execute(SESSIONS[21])
    resumed = again.execute(SESSIONS[21])
    assert resumed.fills == live.fills and [f.isin for f in resumed.fills] == [OTHER]
    assert restored.to_document() == account.to_document()
    assert again.last_buy_fill == book.last_buy_fill


def test_a_tampered_persisted_state_is_refused(tmp_path: Path) -> None:
    clock = FrozenClock(D0)
    switch = switch_at(tmp_path, clock)
    _, account = open_book(
        "FM-SWING-BRK-10L", market=_market(), clock=clock, kill_switch=switch, journal=ListJournal()
    )
    document = account.to_document()
    document["broker"] = {**document["broker"], "cash": "99999999"}
    with pytest.raises(ValueError, match="does not reproduce its digest"):
        M17PaperAccount.restore(
            document,
            account_id=account.account_id,
            market=_market(),
            kill_switch=switch,
            clock=clock,
            schedule=load_repo_rate_schedule(),
            corporate_actions=BookActionCalendar(),
            circuit=NoCircuitData(),
            book_digest=account.book_digest,
        )


# ── paper only: execution.kite_broker is unreachable from M17 ────────────────────────────────────

FIRST_PARTY = frozenset({"analyst", "dataplatform", "execution", "backtest", "accounting"})
M17_ROOTS = ("analyst/fundmanager", "backtest/fm_paper.py")
FORBIDDEN = "execution.kite_broker"


def _module_file(root: Path, name: str) -> Path | None:
    base = root.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(root: Path, path: Path) -> set[str]:
    """Every module ``path`` may import, ``TYPE_CHECKING`` and literal dynamic imports included."""
    name = _module_name(root, path)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if base:
                found.add(base)
            found.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            )
        ):
            found.add(node.args[0].value)
    return found


def kite_paths(root: Path, starts: tuple[str, ...]) -> list[str]:
    """Every first-party import chain from a module under ``starts`` that reaches the real broker.

    Transitive, and through parent packages: importing ``a.b.c`` runs ``a/__init__`` and
    ``a/b/__init__`` too.
    """
    seeds: list[str] = []
    for start in starts:
        target = root / start
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        seeds.extend(_module_name(root, f) for f in files)
    parent: dict[str, str | None] = dict.fromkeys(seeds)
    queue = deque(seeds)
    hits: list[str] = []
    while queue:
        name = queue.popleft()
        if name == FORBIDDEN or name.startswith(FORBIDDEN + "."):
            chain = [name]
            step = parent[name]
            while step is not None:
                chain.append(step)
                step = parent[step]
            hits.append(" <- ".join(chain))
            continue
        path = _module_file(root, name)
        if path is None:
            continue
        pieces = name.split(".")
        nexts = {".".join(pieces[:i]) for i in range(1, len(pieces))} | _imports(root, path)
        for nxt in sorted(nexts):
            if nxt.split(".")[0] in FIRST_PARTY and nxt not in parent:
                parent[nxt] = name
                queue.append(nxt)
    return hits


def test_the_real_broker_is_unreachable_from_m17_by_import() -> None:
    assert kite_paths(REPO, M17_ROOTS) == []


def test_the_reachability_scan_finds_a_planted_path(tmp_path: Path) -> None:
    (tmp_path / "execution").mkdir()
    (tmp_path / "execution" / "__init__.py").write_text("")
    (tmp_path / "execution" / "kite_broker.py").write_text("")
    (tmp_path / "backtest").mkdir()
    (tmp_path / "backtest" / "__init__.py").write_text("")
    (tmp_path / "backtest" / "helper.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from execution import kite_broker\n"
    )
    (tmp_path / "backtest" / "fm_paper.py").write_text("from backtest import helper\n")
    (tmp_path / "analyst" / "fundmanager").mkdir(parents=True)
    (tmp_path / "analyst" / "__init__.py").write_text("")
    (tmp_path / "analyst" / "fundmanager" / "__init__.py").write_text("")
    hits = kite_paths(tmp_path, M17_ROOTS)
    assert hits and "backtest.helper" in hits[0]


def test_loading_every_m17_module_never_loads_the_real_broker() -> None:
    probe = (
        "import sys\n"
        "import analyst.fundmanager, analyst.fundmanager.books, analyst.fundmanager.mandate\n"
        "import backtest.fm_paper\n"
        "loaded = sorted(m for m in sys.modules if 'kite' in m.lower())\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=REPO,
    )
    assert done.returncode == 0, f"{done.stdout[-500:]} {done.stderr[-2000:]}"


def test_the_account_trades_only_on_the_paper_broker(tmp_path: Path) -> None:
    class Lookalike(SimBroker):
        pass

    clock = FrozenClock(D0)
    market = _market()
    sim = Lookalike(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=market,
        opening_cash=Decimal("1000"),
    )
    with pytest.raises(PaperModeViolationError, match="Lookalike"):
        M17PaperAccount(
            account_id="m17_test",
            sim=sim,
            book=_AccountBook(PortfolioBook(Decimal("1000"))),
            accrual=CashInterestAccrual(load_repo_rate_schedule()),
            kill_switch=switch_at(tmp_path, clock),
            clock=clock,
            alerter=RecordingAlerter(),
            corporate_actions=BookActionCalendar(),
            circuit=NoCircuitData(),
            ledger=CorporateActionLedger(D0),
        )


def test_no_m17_book_or_account_api_takes_a_broker() -> None:
    for fn in (M17PaperAccount.open, M17PaperAccount.restore, FundBook, FundDesk):
        for name, parameter in inspect.signature(fn).parameters.items():
            assert "broker" not in name.lower(), f"{fn!r} takes {name}"
            assert "Broker" not in str(parameter.annotation), f"{fn!r}.{name}"
