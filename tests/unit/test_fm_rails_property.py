"""M17.5 — no generated decision stream produces a fill that breaches an M17 rail.

The unit file shows each rail refusing the order it was written for. A rail's claim is universal,
so this file generates the streams: a small market (names, sectors, series, prices, turnover that
ranges from thin to deep), a roster book, and several sessions of buy/sell orders of random size.
Each stream runs through the whole M17 path — `FundBook.decide` (A8's `check_book_order` through
`RailEngine.guard_book_order`), the staging coordinator, the paper broker's fills at the next open,
reconciliation — and an independent oracle, written here from pre-registration §4 step 5 and not
from the engine, checks two things every session:

* **Every decision**: an order is staged exactly when the oracle finds no breach, and a refused
  order's ``RAIL_BLOCK`` names exactly the rails the oracle says it broke. Exact both ways, so a
  rail that refuses too much is caught as surely as one that refuses too little.
* **Every fill**: it fills an order staged the session before, no larger; cash and holdings stay
  non-negative; and valued at the decision session's closes, a bought name is within the position
  cap, its sector within the sector cap, the book within its name count, the order within
  participation, and a sold name was held long enough and held at all.

**Inverting any cap makes it fail.** Every M17 rail compares through `analyst.rails.engine.
_breached`; the inversion tests flip that comparison for one rail at a time and run a fixed
stream that the uninverted rails refuse on every rail at least once — and the property fails.

Offline and deterministic: an in-memory market, a frozen clock, the checked-in rate card and
repo schedule (CLAUDE.md).
"""

from __future__ import annotations

import inspect
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

import analyst.rails.engine as rails_engine
from analyst.fundmanager import load_roster
from analyst.fundmanager.books import BookOrder, DecisionReport, FundBook, median_traded_value
from analyst.journal.models import Decision, JournalEntry
from analyst.rails import BookRails, RailId
from dataplatform.clock import FrozenClock
from execution.broker import Side
from tests.fm_books_support import Bar, FmMarket, ListJournal, open_book, switch_at, weekdays

_HUNDRED = Decimal(100)
LOOKBACK = load_roster().rails.participation_lookback_sessions
#: The first decision session has a full participation lookback behind it.
FIRST_DECISION = LOOKBACK - 1
DECISIONS = 8
SESSIONS = weekdays(date(2025, 3, 3), FIRST_DECISION + DECISIONS + 2)
BOOK_IDS = tuple(b.id for b in (*load_roster().managers, *load_roster().controls))
SECTORS = ("IT", "BANKS", "AUTO", "PHARMA", "FMCG", "METALS", "ENERGY", "TELECOM")


def isin(n: int) -> str:
    return f"INE{n:05d}B010"


# ── the oracle: pre-registration §4 step 5, written independently of the engine ────────────────


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The book as the oracle sees it at a decision session, before the session's orders."""

    session: date
    quantities: dict[str, int]
    cash_value: Decimal
    spendable: Decimal
    closes: dict[str, Decimal]

    @property
    def total(self) -> Decimal:
        return self.cash_value + sum(
            (self.closes[i] * q for i, q in self.quantities.items()), Decimal(0)
        )


@dataclass(slots=True)
class Oracle:
    """Re-derives every rail verdict from the market table and the book, never from A8."""

    rails: BookRails
    market: FmMarket
    last_buy: dict[str, date] = field(default_factory=dict)

    def median(self, name: str, session: date) -> Decimal | None:
        known = [s for s in self.market.sessions if s <= session and (name, s) in self.market.bars]
        window = known[-self.rails.participation_lookback_sessions :]
        if len(window) < self.rails.participation_lookback_sessions:
            return None
        return median_traded_value([self.market.bars[(name, s)].traded_value for s in window])

    def participation_ok(self, name: str, notional: Decimal, session: date) -> bool:
        median = self.median(name, session)
        if median is None or median <= 0:
            return False
        return notional * _HUNDRED <= self.rails.participation_max_pct * median

    def held_long_enough(self, name: str, session: date) -> bool:
        bought = self.last_buy.get(name)
        if bought is None:
            return False
        return self.market.sessions_between(bought, session) >= self.rails.min_hold_sessions

    def verdicts(
        self, snap: Snapshot, orders: Sequence[BookOrder]
    ) -> list[tuple[BookOrder, frozenset[RailId]]]:
        """Each order's breached rails, sells first then buys, in the book's clearing order."""
        out: list[tuple[BookOrder, frozenset[RailId]]] = []
        sellable = dict(snap.quantities)
        for order in (o for o in orders if o.side is Side.SELL):
            notional = snap.closes[order.isin] * order.quantity
            broken: set[RailId] = set()
            if order.quantity > sellable.get(order.isin, 0):
                broken.add(RailId.NO_SHORT)
            if not self.held_long_enough(order.isin, snap.session):
                broken.add(RailId.MIN_HOLD)
            if not self.participation_ok(order.isin, notional, snap.session):
                broken.add(RailId.PARTICIPATION)
            if not broken:
                sellable[order.isin] = sellable.get(order.isin, 0) - order.quantity
            out.append((order, frozenset(broken)))

        held = dict(snap.quantities)
        spendable = snap.spendable
        total = snap.total
        for order in (o for o in orders if o.side is Side.BUY):
            price = snap.closes[order.isin]
            notional = price * order.quantity
            broken = set()
            if self.market.series(order.isin, snap.session) not in self.rails.equity_series:
                broken.add(RailId.NO_FNO)
            if notional > spendable:
                broken.add(RailId.NO_MARGIN)
            if not self.participation_ok(order.isin, notional, snap.session):
                broken.add(RailId.PARTICIPATION)
            after = held.get(order.isin, 0) + order.quantity
            if price * after * _HUNDRED / total > self.rails.max_position_pct:
                broken.add(RailId.MAX_POSITION)
            sector = self.market.sector(order.isin)
            in_sector = sum(
                (
                    snap.closes[i] * (after if i == order.isin else q)
                    for i, q in {**held, order.isin: after}.items()
                    if self.market.sector(i) == sector
                ),
                Decimal(0),
            )
            if in_sector * _HUNDRED / total > self.rails.max_sector_pct:
                broken.add(RailId.MAX_SECTOR)
            if len(set(held) | {order.isin}) > self.rails.max_positions:
                broken.add(RailId.MAX_POSITIONS)
            if not broken:
                held[order.isin] = after
                spendable -= notional
            out.append((order, frozenset(broken)))
        return out

    def check_fills(
        self,
        snap: Snapshot,
        staged: Sequence[BookOrder],
        fills: Sequence[tuple[str, Side, int]],
        after: dict[str, int],
        cash_value: Decimal,
        spendable: Decimal,
    ) -> None:
        """Every fill of the session after ``snap`` against every rail, at ``snap``'s closes."""
        assert cash_value >= 0, f"cash went negative: {cash_value}"
        assert spendable >= 0, f"spendable cash went negative: {spendable}"
        assert all(q > 0 for q in after.values()), f"a non-positive holding: {after}"
        by_key = {(o.isin, o.side): o for o in staged}
        total = snap.total
        bought: set[str] = set()
        for name, side, quantity in fills:
            order = by_key.get((name, side))
            assert order is not None, f"a fill of {name} {side} that no staged order asked for"
            assert quantity <= order.quantity, f"{name} filled {quantity} > staged {order.quantity}"
            notional = snap.closes[name] * quantity
            assert self.participation_ok(name, notional, snap.session), f"PARTICIPATION: {name}"
            if side is Side.SELL:
                assert quantity <= snap.quantities.get(name, 0), f"NO_SHORT: {name}"
                assert self.held_long_enough(name, snap.session), f"MIN_HOLD: {name}"
            else:
                assert self.market.series(name, snap.session) in self.rails.equity_series, (
                    f"NO_FNO: {name}"
                )
                bought.add(name)
        bought_value = sum((snap.closes[n] * q for n, s, q in fills if s is Side.BUY), Decimal(0))
        assert bought_value <= snap.spendable, f"NO_MARGIN: bought {bought_value}"
        for name in bought:
            pct = snap.closes[name] * after.get(name, 0) * _HUNDRED / total
            assert pct <= self.rails.max_position_pct, f"MAX_POSITION: {name} at {pct}%"
            sector = self.market.sector(name)
            sector_value = sum(
                (
                    snap.closes[i] * q
                    for i, q in after.items()
                    if self.market.sector(i) == sector and i in snap.closes
                ),
                Decimal(0),
            )
            assert sector_value * _HUNDRED / total <= self.rails.max_sector_pct, (
                f"MAX_SECTOR: {sector}"
            )
        if bought - set(snap.quantities):
            assert len(after) <= self.rails.max_positions, f"MAX_POSITIONS: {len(after)} names"


# ── the harness: one stream through the whole M17 path ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Stream:
    book_id: str
    market: FmMarket
    #: Per decision session: (name, side, share quantity), at most one order per name.
    decisions: tuple[tuple[tuple[str, Side, int], ...], ...]


@dataclass(slots=True)
class StreamResult:
    """How often each rail refused an order, and how many fills the stream produced."""

    refusals: dict[RailId, int] = field(default_factory=dict)
    fills: int = 0


def run_stream(stream: Stream, tmp: Path) -> StreamResult:
    """Run ``stream`` and check every decision and fill against the oracle."""
    clock = FrozenClock(SESSIONS[0])
    journal = ListJournal()
    book, account = open_book(
        stream.book_id,
        market=stream.market,
        clock=clock,
        kill_switch=switch_at(tmp, clock),
        journal=journal,
    )
    oracle = Oracle(book.rails, stream.market)
    result = StreamResult()
    previous: tuple[Snapshot, tuple[BookOrder, ...]] | None = None
    decision_sessions = SESSIONS[FIRST_DECISION : FIRST_DECISION + len(stream.decisions)]
    for index, session in enumerate(
        [*decision_sessions, SESSIONS[FIRST_DECISION + len(stream.decisions)]]
    ):
        clock.freeze_at(session)
        executed = book.execute(session)
        assert executed.recon is not None and executed.recon.ok, "reconciliation broke"
        fills = [(f.isin, f.side, f.quantity) for f in executed.fills]
        result.fills += len(fills)
        if previous is not None:
            snap, staged = previous
            oracle.check_fills(
                snap,
                staged,
                fills,
                dict(account.quantities()),
                account.cash_value,
                account.spendable_cash,
            )
        else:
            assert fills == []
        for fill in executed.fills:
            if fill.side is Side.BUY:
                oracle.last_buy[fill.isin] = fill.session
        if index == len(stream.decisions):
            break
        snap = Snapshot(
            session=session,
            quantities=dict(account.quantities()),
            cash_value=account.cash_value,
            spendable=account.spendable_cash,
            closes={
                name: bar.close for (name, day), bar in stream.market.bars.items() if day == session
            },
        )
        orders = tuple(
            BookOrder(name, side, quantity, f"generated {side.value.lower()}")
            for name, side, quantity in stream.decisions[index]
        )
        expected = oracle.verdicts(snap, orders)
        mark = len(journal.entries)
        report = book.decide(session, orders)
        _compare(report, expected, journal.entries[mark:], result.refusals)
        previous = (snap, tuple(order for order, _ in report.staged))
    return result


def _compare(
    report: DecisionReport,
    expected: list[tuple[BookOrder, frozenset[RailId]]],
    entries: Sequence[JournalEntry],
    refusals: dict[RailId, int],
) -> None:
    staged = [order for order, _ in report.staged]
    should = [order for order, broken in expected if not broken]
    assert staged == should, f"staged {staged} but the rails allow exactly {should}"
    blocks = {
        e.isin: frozenset(RailId(r) for r in e.payload["rails"].split(","))
        for e in entries
        if e.decision is Decision.RAIL_BLOCK
    }
    for order, broken in expected:
        if broken:
            assert blocks.get(order.isin) == broken, (
                f"{order.side.value} {order.quantity} {order.isin}: journaled "
                f"{sorted(blocks.get(order.isin, ()))}, the rails say {sorted(broken)}"
            )
            for rail in broken:
                refusals[rail] = refusals.get(rail, 0) + 1
    assert set(blocks) == {o.isin for o, broken in expected if broken}


# ── generated streams ────────────────────────────────────────────────────────────────────────────

#: Turnover levels from thin (₹5 lakh/day, where participation binds on any book) to deep.
LIQUIDITY = (Decimal("500000"), Decimal("5000000"), Decimal("20000000"), Decimal("5000000000"))


@st.composite
def streams(draw: st.DrawFn) -> Stream:
    book_id = draw(st.sampled_from(BOOK_IDS))
    # Small universes, and ones of 22-30 names a 15- or 20-name book can fill up on, where
    # MAX_POSITIONS and NO_MARGIN bind.
    count = draw(st.one_of(st.integers(min_value=3, max_value=8), st.integers(22, 30)))
    names = [isin(i + 1) for i in range(count)]
    sectors = {n: draw(st.sampled_from(SECTORS)) for n in names}
    series = {n: draw(st.sampled_from(("EQ",) * 5 + ("BE",))) for n in names}
    level = {n: draw(st.sampled_from(LIQUIDITY + LIQUIDITY[-1:] * 3)) for n in names}
    base = {n: Decimal(draw(st.integers(min_value=20, max_value=3000))) for n in names}
    rng = random.Random(draw(st.integers(min_value=0, max_value=2**32 - 1)))
    bars: dict[tuple[str, date], Bar] = {}
    for name in names:
        close = base[name]
        for session in SESSIONS:
            # Moves of up to ±3% in basis points, as exact Decimals (no float reaches a price).
            opening = (close * (1 + Decimal(rng.randint(-300, 300)) / 10000)).quantize(
                Decimal("0.05")
            )
            close = (opening * (1 + Decimal(rng.randint(-300, 300)) / 10000)).quantize(
                Decimal("0.05")
            )
            turnover = (level[name] * Decimal(rng.randint(30, 170)) / 100).quantize(Decimal(1))
            bars[(name, session)] = Bar(
                max(opening, Decimal("1")), max(close, Decimal("1")), turnover, series[name]
            )
    market = FmMarket(SESSIONS, bars, sectors)
    capital = load_roster().get(book_id).opening_capital_inr
    decisions: list[tuple[tuple[str, Side, int], ...]] = []
    for index in range(draw(st.integers(min_value=2, max_value=DECISIONS))):
        session = SESSIONS[FIRST_DECISION + index]
        # A sweep buys every name at once — the session that fills a book up to its name count
        # and its cash, where MAX_POSITIONS and NO_MARGIN bind. Otherwise a random mix.
        if draw(st.sampled_from(("mixed", "mixed", "sweep"))) == "sweep":
            picks = [
                (name, Side.BUY, draw(st.integers(min_value=15, max_value=60))) for name in names
            ]
        else:
            picks = draw(
                st.lists(
                    st.tuples(
                        st.sampled_from(names),
                        st.sampled_from((Side.BUY, Side.BUY, Side.SELL)),
                        st.integers(min_value=1, max_value=110),  # tenths of a % of capital
                    ),
                    max_size=14,
                    unique_by=lambda pick: pick[0],
                )
            )
        day: list[tuple[str, Side, int]] = []
        for name, side, tenths in picks:
            price = bars[(name, session)].close
            quantity = max(1, int(capital * tenths / 1000 / price))
            day.append((name, side, quantity))
        decisions.append(tuple(day))
    return Stream(book_id, market, tuple(decisions))


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(stream=streams())
def test_no_generated_decision_stream_produces_a_fill_that_breaches_a_rail(
    stream: Stream, tmp_path_factory: pytest.TempPathFactory
) -> None:
    result = run_stream(stream, tmp_path_factory.mktemp("switch"))
    for rail in result.refusals:
        event(f"refused by {rail.value}")
    event("orders filled" if result.fills else "nothing filled")


# ── a fixed stream every rail refuses something in, and the inversions it catches ──────────────

_LIQUID = Decimal("5000000000")  # ₹500 cr/day
_THIN = Decimal("1000000")  # ₹10 lakh/day: 5% is ₹50,000


def gauntlet() -> Stream:
    """A ₹10 lakh book's stream in which each M17 rail refuses at least one order.

    D0: X at 11% (MAX_POSITION); THIN for ₹60,000 against a ₹50,000 ceiling (PARTICIPATION); a BE
    name (NO_FNO); a sale of an unheld name (NO_SHORT); three 9.5% names of one sector and a
    fourth (MAX_SECTOR); a 1% buy of A that passes. D1: a sale of A the session it filled
    (MIN_HOLD); eleven 1% names to fifteen and a twelfth (MAX_POSITIONS); a buy beyond spendable
    cash (NO_MARGIN). D3: the sale of A two sessions after its fill, which passes.
    """
    names = {
        "A": ("IT", "EQ", _LIQUID),
        "X": ("AUTO", "EQ", _LIQUID),
        "THIN": ("AUTO", "EQ", _THIN),
        "BE": ("METALS", "BE", _LIQUID),
        "Z": ("IT", "EQ", _LIQUID),
        "P": ("SS", "EQ", _LIQUID),
        "Q": ("SS", "EQ", _LIQUID),
        "R": ("SS", "EQ", _LIQUID),
        "S": ("SS", "EQ", _LIQUID),
        "Y": ("PHARMA", "EQ", _LIQUID),
        **{f"L{i:02d}": (SECTORS[i % 4] + "-L", "EQ", _LIQUID) for i in range(1, 13)},
    }
    ids = {label: isin(100 + n) for n, label in enumerate(names)}
    bars = {
        (ids[label], session): Bar(Decimal(100), Decimal(100), turnover, series)
        for label, (_, series, turnover) in names.items()
        for session in SESSIONS
    }
    sectors = {ids[label]: sector for label, (sector, _, _) in names.items()}
    buy, sell = Side.BUY, Side.SELL
    d0 = (
        (ids["X"], buy, 1100),
        (ids["THIN"], buy, 600),
        (ids["BE"], buy, 10),
        (ids["Z"], sell, 10),
        (ids["P"], buy, 950),
        (ids["Q"], buy, 950),
        (ids["R"], buy, 950),
        (ids["S"], buy, 400),
        (ids["A"], buy, 100),
    )
    d1 = (
        (ids["A"], sell, 10),
        *((ids[f"L{i:02d}"], buy, 100) for i in range(1, 13)),
        (ids["Y"], buy, 6000),
    )
    d3 = ((ids["A"], sell, 10),)
    return Stream("FM-SWING-10L", FmMarket(SESSIONS, bars, sectors), (d0, d1, (), d3))


M17_RAILS = (
    RailId.MAX_POSITION,
    RailId.MAX_SECTOR,
    RailId.MAX_POSITIONS,
    RailId.PARTICIPATION,
    RailId.MIN_HOLD,
    RailId.NO_SHORT,
    RailId.NO_FNO,
    RailId.NO_MARGIN,
)


def test_the_gauntlet_is_refused_by_every_rail_and_passes_the_property(tmp_path: Path) -> None:
    result = run_stream(gauntlet(), tmp_path)
    assert set(result.refusals) == set(M17_RAILS), result.refusals
    assert result.fills > 0


def _inverted(rail: RailId) -> Callable[..., bool]:
    real = rails_engine._breached

    def breached(which: RailId, observed: Decimal, limit: Decimal, *, floor: bool = False) -> bool:
        verdict = real(which, observed, limit, floor=floor)
        return not verdict if which is rail else verdict

    return breached


@pytest.mark.parametrize("rail", M17_RAILS, ids=lambda r: r.value)
def test_inverting_any_rail_makes_the_property_fail(
    rail: RailId, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rails_engine, "_breached", _inverted(rail))
    with pytest.raises(AssertionError):
        run_stream(gauntlet(), tmp_path)


def test_the_oracle_is_not_the_engine() -> None:
    """The oracle never calls A8: inverting the engine cannot invert the oracle with it."""
    source = inspect.getsource(Oracle)
    assert "rails_engine" not in source and "check_book_order" not in source
    assert FundBook.decide.__module__ == "analyst.fundmanager.books"
