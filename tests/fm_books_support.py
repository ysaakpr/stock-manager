"""Test support for the M17 books: an in-memory market both the book and the paper broker read.

`FmMarket` is a `SessionMarket` (next session, reference bars — what the paper ``SimBroker`` fills
from) and an `analyst.fundmanager.books.BookMarket` (closes, series, sectors, traded values, the
session count) over one table, so a test states a market once and both sides see the same facts.
Every answer is as of the session asked about; `traded_values` never returns a later session
unless a test builds a deliberately leaking market (`leak_future`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from analyst.fundmanager import ControlMandate, ManagerMandate, StyleMandate, load_roster
from analyst.fundmanager.books import (
    BookJournal,
    FundBook,
    book_rails,
    m17_kill_switch,
    paper_account_id,
)
from analyst.journal.evidence import EvidenceBundle
from analyst.journal.models import JournalEntry
from analyst.rails import BookRails
from backtest.book_actions import BookActionCalendar, BookActionSource
from backtest.cash_interest import load_repo_rate_schedule
from backtest.fm_circuit import CircuitMarket, NoCircuitData
from backtest.fm_paper import M17PaperAccount
from dataplatform.clock import FrozenClock
from execution.broker import Exchange
from execution.kill_switch import KillSwitch
from execution.recon import RecordingAlerter
from execution.sim_broker import NoReferenceBarError, ReferenceBar


def weekdays(start: date, count: int) -> list[date]:
    """``count`` Monday-to-Friday sessions from ``start`` (a test calendar, no holidays)."""
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@dataclass(frozen=True, slots=True)
class Bar:
    """One name on one session: its open (the fill reference), close (the mark), turnover."""

    open: Decimal
    close: Decimal
    traded_value: Decimal
    series: str = "EQ"


class FmMarket:
    """A `SessionMarket` and a `BookMarket` over one ``(isin, session) -> Bar`` table."""

    def __init__(
        self,
        sessions: Sequence[date],
        bars: Mapping[tuple[str, date], Bar],
        sectors: Mapping[str, str],
        *,
        leak_future: bool = False,
    ) -> None:
        self.sessions = sorted(sessions)
        self.bars = dict(bars)
        self.sectors = dict(sectors)
        self._leak = leak_future

    # SessionMarket
    def next_session(self, after: date) -> date:
        for session in self.sessions:
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        bar = self.bars.get((isin, session))
        if bar is None:
            raise NoReferenceBarError(f"no bar for {isin} on {session.isoformat()}")
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=bar.open,
            vwap=bar.open,
            traded_value=bar.traded_value,
        )

    # BookMarket
    def close(self, isin: str, session: date) -> Decimal | None:
        bar = self.bars.get((isin, session))
        return None if bar is None else bar.close

    def series(self, isin: str, session: date) -> str | None:
        bar = self.bars.get((isin, session))
        return None if bar is None else bar.series

    def sector(self, isin: str) -> str:
        return self.sectors[isin]

    def traded_values(
        self, isin: str, *, through: date, sessions: int
    ) -> Sequence[tuple[date, Decimal]]:
        # A leaking market answers from the whole table, later sessions included — the PIT bug the
        # book must refuse, built on purpose so the refusal has something to catch.
        horizon = self.sessions if self._leak else [s for s in self.sessions if s <= through]
        known = [(s, self.bars[(isin, s)].traded_value) for s in horizon if (isin, s) in self.bars]
        return known[-sessions:]

    def sessions_between(self, start: date, end: date) -> int:
        return sum(1 for s in self.sessions if start < s <= end)


class ListJournal:
    """A `BookJournal` keeping entries (and evidence bundles) in memory, in append order."""

    def __init__(self) -> None:
        self.entries: list[JournalEntry] = []
        self.bundles: list[EvidenceBundle] = []

    def append(self, entry: JournalEntry, *, evidence: EvidenceBundle | None = None) -> object:
        if evidence is not None:
            self.bundles.append(evidence)
        self.entries.append(entry)
        return entry


def roster_rails(book_id: str) -> tuple[BookRails, Decimal]:
    """The roster's rails and opening capital for ``book_id`` (any book but the bench)."""
    roster = load_roster()
    mandate = roster.get(book_id)
    assert isinstance(mandate, ManagerMandate | ControlMandate | StyleMandate), "no bench account"
    return book_rails(mandate, roster.rails), mandate.opening_capital_inr


def open_book(
    book_id: str,
    *,
    market: FmMarket,
    clock: FrozenClock,
    kill_switch: KillSwitch,
    journal: BookJournal,
    rails: BookRails | None = None,
    opening_cash: Decimal | None = None,
    corporate_actions: BookActionSource | None = None,
    circuit: CircuitMarket | None = None,
) -> tuple[FundBook, M17PaperAccount]:
    """One M17 book on a fresh paper account, rails from the roster unless given.

    No corporate actions and no circuit data unless given — each test that is about them says so.
    """
    roster_book_rails, capital = roster_rails(book_id)
    account = M17PaperAccount.open(
        account_id=paper_account_id(book_id),
        opening_cash=capital if opening_cash is None else opening_cash,
        market=market,
        kill_switch=kill_switch,
        clock=clock,
        schedule=load_repo_rate_schedule(),
        corporate_actions=BookActionCalendar() if corporate_actions is None else corporate_actions,
        circuit=NoCircuitData() if circuit is None else circuit,
        alerter=RecordingAlerter(),
    )
    book = FundBook(
        book_id=book_id,
        rails=roster_book_rails if rails is None else rails,
        account=account,
        market=market,
        journal=journal,
        kill_switch=kill_switch,
        clock=clock,
    )
    return book, account


def switch_at(tmp: Path, clock: FrozenClock) -> KillSwitch:
    """A fresh, armed kill switch file under ``tmp``."""
    return m17_kill_switch(tmp, clock=clock)
