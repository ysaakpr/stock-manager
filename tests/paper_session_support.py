"""Test support: a frozen fixture market for the daily paper session (M13.1).

``FixtureWorld`` is a ``backtest.paper_session.PaperWorld`` over a hand-built fortnight around the
2026 Gandhi Jayanti holiday (Friday 2026-10-02): a holiday-aware calendar, ten NSE names with
deterministic bars, a momentum signal per session whose ranking turns over at the October
rebalance, and a risk-on regime reading. Everything is a pure function of the date and the ISIN,
so two worlds built the same way are indistinguishable and a session over one is byte-reproducible.

Knobs a test turns: ``unpriced`` (sessions whose bars are missing — prices not in L1),
``no_regime`` (sessions the published index has no level for), ``risk_off`` (sessions the regime
filter reads as below its moving average). ``reads`` records every signal/regime read.

``fixture_spec`` is the book under test: the D13-ratified parameters (or ones a test passes) under
the ratified rail *numbers* (15 % position, 35 % sector, 8 names, ₹1.2 L per order) with a sector
map over the fixture names, so A8 really clears every order.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from analyst.cases import RiskRails
from backtest.book_actions import BookActionCalendar, BookActionSource
from backtest.paper_session import PaperBookSpec
from backtest.policies.momentum_v2 import (
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Record,
    RegimeReading,
)
from backtest.rails import BacktestRailPolicy, SectorMap
from backtest.run import RegimeSourceError
from dataplatform.query.pit import Dataset
from execution.broker import Exchange
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SessionMarket

#: Ten NSE names, ISIN-shaped, each with a fixed base price and volatility.
ISINS: tuple[str, ...] = tuple(
    f"INE{n:03d}A01{n % 10}{(n * 7) % 10}{(n * 3) % 10}" for n in range(1, 11)
)
SECTORS: Mapping[str, str] = {
    isin: ("banks", "it", "energy", "fmcg")[index % 4] for index, isin in enumerate(ISINS)
}

#: 2026-10-02 (Gandhi Jayanti) is an NSE holiday; the fixture fortnight straddles it.
HOLIDAY = date(2026, 10, 2)
FIRST = date(2026, 9, 28)
LAST = date(2026, 10, 30)
SEPT_LAST = date(2026, 9, 30)
OCT_FIRST = date(2026, 10, 1)
OCT_SECOND = date(2026, 10, 5)
OCT_THIRD = date(2026, 10, 6)
OCT_FOURTH = date(2026, 10, 7)

FIXTURE_CASH = Decimal("1000000")


def calendar_sessions(start: date = FIRST, end: date = LAST) -> list[date]:
    out: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and day != HOLIDAY:
            out.append(day)
        day += timedelta(days=1)
    return out


def _price(isin: str, session: date) -> Decimal:
    index = ISINS.index(isin)
    drift = Decimal((session - FIRST).days) * Decimal("0.25")
    return (Decimal(100 + 15 * index) + drift).quantize(Decimal("0.01"))


def _momentum(isin: str, session: date) -> Decimal:
    """September ranks the low-index names first; from October the ranking reverses."""
    index = ISINS.index(isin)
    rank_key = index if session < OCT_FIRST else len(ISINS) - 1 - index
    return Decimal("0.60") - Decimal(rank_key) * Decimal("0.05")


def _volatility(isin: str) -> Decimal:
    return Decimal("0.20") + Decimal(ISINS.index(isin) % 3) * Decimal("0.02")


@dataclass
class FixtureWorld:
    """A ``PaperWorld`` over the fixture fortnight (see the module docstring)."""

    unpriced: set[date] = field(default_factory=set)
    no_regime: set[date] = field(default_factory=set)
    risk_off: set[date] = field(default_factory=set)
    rebalance_on: set[date] | None = None
    reads: list[tuple[str, date]] = field(default_factory=list)

    def is_session(self, day: date) -> bool:
        return day in set(calendar_sessions())

    def sessions(self, start: date, end: date) -> Sequence[date]:
        return [day for day in calendar_sessions() if start <= day <= end]

    def prices_ready(self, day: date) -> bool:
        return day not in self.unpriced

    def closes(self, session: date) -> dict[str, Decimal]:
        if session in self.unpriced:
            return {}
        return {isin: _price(isin, session) for isin in ISINS}

    def market(
        self, *, first: date, through: date, held: Callable[[], Iterable[str]]
    ) -> SessionMarket:
        return _FixtureMarket(self)

    def marks(self, held: Callable[[], Iterable[str]]) -> Callable[[date], Mapping[str, Decimal]]:
        return self.closes

    def momentum_data(self, day: date, parameters: MomentumV2Parameters) -> _FixtureMomentum:
        return _FixtureMomentum(self)

    def corporate_actions(self) -> BookActionSource | None:
        return BookActionCalendar(())


class _FixtureMarket:
    def __init__(self, world: FixtureWorld) -> None:
        self._world = world

    def next_session(self, after: date) -> date:
        for session in calendar_sessions():
            if session > after:
                return session
        raise NoReferenceBarError(f"no session after {after.isoformat()}")

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        if session in self._world.unpriced or isin not in ISINS:
            raise NoReferenceBarError(f"no bar for {isin} on {session.isoformat()}")
        price = _price(isin, session)
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=price,
            vwap=price,
            traded_value=Decimal("5000000000"),
        )


class _FixtureMomentum:
    """``MomentumV2Data`` over the fixture; ``is_rebalance`` is the first session of each month."""

    def __init__(self, world: FixtureWorld) -> None:
        self._world = world

    def is_rebalance(self, session: date) -> bool:
        if self._world.rebalance_on is not None:
            return session in self._world.rebalance_on
        earlier = [day for day in calendar_sessions() if day < session]
        return not earlier or (earlier[-1].year, earlier[-1].month) != (session.year, session.month)

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        self._world.reads.append(("signal", as_of))
        records = (
            ()
            if as_of in self._world.unpriced
            else tuple(
                MomentumV2Record(
                    isin=isin,
                    momentum_0_12=_momentum(isin, as_of),
                    momentum_12_1=_momentum(isin, as_of),
                    price=_price(isin, as_of),
                    volatility=_volatility(isin),
                    knowable_date=as_of,
                )
                for isin in ISINS
            )
        )
        return Dataset.declaring(
            f"fixture_momentum@{as_of.isoformat()}",
            records,
            knowable_date=lambda r: r.knowable_date,
        )

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        self._world.reads.append(("regime", as_of))
        if as_of in self._world.no_regime:
            raise RegimeSourceError(f"the published 'nifty50' series has no level for {as_of}")
        level = Decimal("90") if as_of in self._world.risk_off else Decimal("110")
        reading = RegimeReading(
            index_level=level, moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(
            f"fixture_regime@{as_of.isoformat()}",
            (reading,),
            knowable_date=lambda r: r.knowable_date,
        )


def fixture_rail_policy() -> BacktestRailPolicy:
    """The ratified rail numbers over the fixture's sector map."""
    return BacktestRailPolicy(
        policy_id="paper-fixture",
        version=1,
        rails=RiskRails(
            max_position_pct=Decimal("15"),
            max_sector_pct=Decimal("35"),
            min_holdings=8,
            drawdown_review_pct=Decimal("25"),
            max_order_value_inr=Decimal("120000"),
            max_order_pct_of_case=Decimal("15"),
        ),
        sectors=SectorMap(source="fixture", sha256="fixture", by_isin=dict(SECTORS)),
        provenance="test-only: the ratified rail numbers over the paper-session fixture names",
    )


def fixture_spec(parameters: MomentumV2Parameters = PAPER_RATIFIED_2026_09_06) -> PaperBookSpec:
    return PaperBookSpec(
        book_id="paper_fixture_book",
        parameters=parameters,
        opening_cash=FIXTURE_CASH,
        rail_policy=fixture_rail_policy(),
    )
