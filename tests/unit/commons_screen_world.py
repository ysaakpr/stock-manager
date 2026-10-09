"""A synthetic lake for the M17.9 screens, dossier and base-rate tests (no network, no lake).

`ScreenWorld` holds every record a :class:`~analyst.commons.inputs.ScreenSource` serves, and
`FakeScreenSource` serves it **unfiltered** by date, so a future-dated record reaches the PIT guard
instead of being quietly dropped by the fake. `screen_world()` lays out named price paths on 300
weekday sessions; `sheets_for()` builds the M17.1 sheets of that world by hand (only what the
screens read), with real digests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from analyst.commons.digests import AnnouncementText
from analyst.commons.inputs import (
    CorporateActionNotice,
    DealRecord,
    FoReading,
    PriceBandEntry,
    PriceBar,
)
from analyst.commons.sheets import (
    SHEET_VERSION,
    BreadthReading,
    CommonsSheets,
    EquityBar,
    FilingFact,
    IndexLevel,
    MarketSheet,
    SourceUnavailableError,
    SurveillanceEntry,
    UniverseParameters,
    UniverseRow,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.query import Dataset
from tests.unit.test_commons_sheets import _isin, _weekdays

SESSION = date(2026, 10, 8)
CALENDAR = _weekdays(SESSION, 300)
N = len(CALENDAR)
PARAMS = UniverseParameters(
    series="EQ",
    min_median_traded_value_inr=Decimal(10_000_000),
    median_lookback_sessions=20,
    excluded_surveillance=("GSM", "ESM"),
    flagged_surveillance=("ASM",),
)
INDEX = "IN.NSE.NIFTY_500.CLOSE"

LEADER, FLAGGED, BANDED, BREAKOUT = (_isin(n) for n in (501, 502, 503, 504))
FILLERS = tuple(_isin(n) for n in range(510, 518))
NAMES = (LEADER, FLAGGED, BANDED, BREAKOUT, *FILLERS)
SECTOR = {LEADER: "Tech", FLAGGED: "Tech", BANDED: "Banks", BREAKOUT: "Autos"} | {
    isin: ("Banks" if k % 2 else "Tech") for k, isin in enumerate(FILLERS)
}
#: The next NSE sessions after SESSION, passed explicitly so no test reads the holiday file.
FUTURE_SESSIONS = tuple(
    d for d in (SESSION + timedelta(days=k) for k in range(1, 120)) if d.weekday() < 5
)[:60]


def clock(day: date = SESSION, hour: int = 22) -> FrozenClock:
    return FrozenClock(datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST))


def pbar(
    isin: str,
    day: date,
    close: Decimal,
    *,
    rng: Decimal = Decimal("0.01"),
    volume: int = 200_000,
    deliv: int | None = None,
) -> PriceBar:
    close = close.quantize(Decimal("0.0001"))
    return PriceBar(
        isin=isin,
        trade_date=day,
        high=(close * (1 + rng)).quantize(Decimal("0.0001")),
        low=(close * (1 - rng)).quantize(Decimal("0.0001")),
        close=close,
        volume=Decimal(volume),
        raw_close=close,
        traded_value=close * volume,
        size=close * volume,
        deliv_qty=None if deliv is None else Decimal(deliv),
        deliv_pct=None if deliv is None else Decimal("40"),
    )


def _zig(i: int, amp: str) -> Decimal:
    return Decimal(1) + Decimal(amp) * (1 if i % 2 else -1)


def trend(isin: str, growth: str, *, amp: str = "0.003", base: int = 100) -> list[PriceBar]:
    g = Decimal(growth)
    return [
        pbar(isin, day, Decimal(base) * g**i * _zig(i, amp), deliv=80_000 + (i % 2) * 20_000)
        for i, day in enumerate(CALENDAR)
    ]


def breakout(isin: str) -> list[PriceBar]:
    """Wide ranges, then ten tight sessions, then a close above every close of 60, on 3x volume."""
    out: list[PriceBar] = []
    for i, day in enumerate(CALENDAR):
        if i == N - 1:
            out.append(pbar(isin, day, Decimal(103), rng=Decimal("0.01"), volume=600_000))
        elif i >= N - 11:
            out.append(pbar(isin, day, Decimal(100) * _zig(i, "0.002"), rng=Decimal("0.002")))
        else:
            out.append(pbar(isin, day, Decimal(100) * _zig(i, "0.015"), rng=Decimal("0.03")))
    return out


def raw_of(bar: PriceBar) -> EquityBar:
    return EquityBar(
        isin=bar.isin,
        trade_date=bar.trade_date,
        close=bar.raw_close,
        traded_qty=int(bar.volume),
        traded_value=bar.traded_value,
        deliv_qty=None if bar.deliv_qty is None else int(bar.deliv_qty),
        deliv_pct=bar.deliv_pct,
    )


def ann(isin: str, ref: str, day: date, subject: str, body: str | None = None) -> AnnouncementText:
    return AnnouncementText(
        isin=isin,
        ref=ref,
        ts=datetime(day.year, day.month, day.day, 15, 0, tzinfo=IST),
        knowable_date=day,
        category=None,
        subject=subject,
        body=body,
        attachment_ref=None,
    )


def shares_fact(isin: str, filed: date, count: str = "10000000") -> FilingFact:
    return FilingFact(
        isin=isin,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        filing_date=filed,
        filing_id=f"F-{isin}",
        nature=Nature.STANDALONE,
        concept="shares_outstanding",
        segment=None,
        value=Decimal(count),
    )


@dataclass
class ScreenWorld:
    calendar: list[date]
    bars: list[PriceBar] = field(default_factory=list)
    levels: list[IndexLevel] = field(default_factory=list)
    filings: list[FilingFact] = field(default_factory=list)
    announcements: list[AnnouncementText] = field(default_factory=list)
    surveillance: dict[str, list[SurveillanceEntry]] = field(default_factory=dict)
    bands: list[PriceBandEntry] = field(default_factory=list)
    deals: list[DealRecord] = field(default_factory=list)
    fo: list[FoReading] = field(default_factory=list)
    actions: list[CorporateActionNotice] = field(default_factory=list)


class FakeScreenSource:
    """Serves the world unfiltered by date (the guard selects); filters by name and session."""

    def __init__(self, world: ScreenWorld, *, missing: frozenset[str] = frozenset()) -> None:
        self.world = world
        self.missing = missing

    def _serve[R](
        self, name: str, records: Sequence[R], knowable: Callable[[R], date]
    ) -> Dataset[R]:
        if name in self.missing:
            raise SourceUnavailableError(name, "not built in this fixture")
        return Dataset.declaring(name, list(records), knowable_date=knowable)

    def sessions(self, through: date, count: int) -> Dataset[date]:
        upto = [d for d in self.world.calendar if d <= through]
        return self._serve("sessions", upto[-count:], lambda d: d)

    def equity_bars(self, sessions: Sequence[date], series: str) -> Dataset[EquityBar]:
        wanted = set(sessions)
        rows = [
            raw_of(b)
            for b in self.world.bars
            if b.trade_date in wanted or b.trade_date > max(wanted)
        ]
        return self._serve("equity_bars", rows, lambda b: b.trade_date)

    def price_bars(
        self, isins: frozenset[str], sessions: Sequence[date], series: str
    ) -> Dataset[PriceBar]:
        wanted = set(sessions)
        rows = [
            b
            for b in self.world.bars
            if b.isin in isins and (b.trade_date in wanted or b.trade_date > max(wanted))
        ]
        return self._serve("price_bars", rows, lambda b: b.trade_date)

    def index_levels(self, series_ids: Sequence[str], through: date) -> Dataset[IndexLevel]:
        return self._serve("index_levels", self.world.levels, lambda lv: lv.knowable_date)

    def filings(self, through: date) -> Dataset[FilingFact]:
        return self._serve("filings", self.world.filings, lambda f: f.filing_date)

    def concept_facts(self, concepts: frozenset[str], through: date) -> Dataset[FilingFact]:
        rows = [f for f in self.world.filings if f.concept in concepts]
        return self._serve("concept_facts", rows, lambda f: f.filing_date)

    def announcement_texts(self, start: date, through: date) -> Dataset[AnnouncementText]:
        rows = [a for a in self.world.announcements if a.knowable_date >= start]
        return self._serve("announcements", rows, lambda a: a.knowable_date)

    def surveillance(self, stage: str, through: date) -> Dataset[SurveillanceEntry]:
        if stage not in self.world.surveillance:
            raise SourceUnavailableError(f"nse_{stage.lower()}_list", "no snapshot")
        return self._serve(
            f"surveillance:{stage}", self.world.surveillance[stage], lambda e: e.knowable_date
        )

    def price_bands(self, through: date) -> Dataset[PriceBandEntry]:
        return self._serve("price_bands", self.world.bands, lambda b: b.knowable_date)

    def deals(self, start: date, through: date) -> Dataset[DealRecord]:
        rows = [d for d in self.world.deals if d.trade_date >= start]
        return self._serve("deals", rows, lambda d: d.trade_date)

    def fo_readings(self, sessions: Sequence[date]) -> Dataset[FoReading]:
        wanted = set(sessions)
        rows = [r for r in self.world.fo if r.trade_date in wanted]
        return self._serve("fo_aggregates", rows, lambda r: r.trade_date)

    def corporate_actions(self, start: date, through: date) -> Dataset[CorporateActionNotice]:
        return self._serve("corporate_actions", self.world.actions, lambda n: n.knowable_date)


def index_levels(calendar: Sequence[date], growth: str = "1.0005") -> list[IndexLevel]:
    g = Decimal(growth)
    return [
        IndexLevel(
            INDEX, day, (Decimal(20000) * g**i * _zig(i, "0.002")).quantize(Decimal("0.01")), day
        )
        for i, day in enumerate(calendar)
    ]


def screen_world() -> ScreenWorld:
    world = ScreenWorld(calendar=list(CALENDAR))
    world.bars += trend(LEADER, "1.004")
    world.bars += trend(FLAGGED, "1.0045")
    world.bars += trend(BANDED, "1.0042")
    world.bars += breakout(BREAKOUT)
    for k, isin in enumerate(FILLERS):
        world.bars += trend(isin, f"1.000{k}", amp=f"0.00{k + 2}")
    world.levels = index_levels(CALENDAR)
    world.filings = [shares_fact(LEADER, CALENDAR[-40])]
    world.announcements = [
        ann(
            FLAGGED,
            "9001",
            CALENDAR[-10],
            "Resignation of Statutory Auditor",
            "Flagged Ltd has informed the Exchange about Resignation of Statutory Auditor",
        ),
        ann(
            LEADER,
            "9002",
            SESSION,
            "Bagging/Receiving of orders/contracts",
            "Leader Ltd has informed the Exchange about Bagging/Receiving of orders/contracts",
        ),
        ann(
            BREAKOUT,
            "9003",
            CALENDAR[-2],
            "Board Meeting Intimation",
            f"Breakout Ltd has informed the Exchange about Board Meeting to be held on "
            f"{FUTURE_SESSIONS[3]:%d-%b-%Y} to consider and approve the financial results.",
        ),
        ann(
            LEADER,
            "9004",
            CALENDAR[-30],
            "Appointment",
            "Leader Ltd has informed the Exchange regarding Appointment of Mr A as CFO.",
        ),
    ]
    outsider = _isin(999)
    world.surveillance = {
        stage: [SurveillanceEntry(isin=outsider, stage=stage, knowable_date=SESSION)]
        for stage in ("GSM", "ESM")
    }
    world.bands = [
        PriceBandEntry(isin=BANDED, series="EQ", band_pct=Decimal(5), knowable_date=SESSION),
        PriceBandEntry(isin=LEADER, series="EQ", band_pct=Decimal(20), knowable_date=SESSION),
        PriceBandEntry(isin=FILLERS[0], series="EQ", band_pct=None, knowable_date=SESSION),
        PriceBandEntry(isin=FILLERS[1], series="BE", band_pct=Decimal(2), knowable_date=SESSION),
    ]
    world.deals = [
        DealRecord(
            isin=LEADER,
            deal_type="BULK",
            trade_date=SESSION,
            client_name="SOME FUND",
            side="BUY",
            quantity=100_000,
            price=Decimal("150.25"),
        )
    ]
    world.fo = [
        FoReading(LEADER, CALENDAR[-2], Decimal(100), 50_000, 100, Decimal("0.8"), Decimal("0.1")),
        FoReading(LEADER, SESSION, Decimal(101), 50_500, 500, Decimal("0.9"), Decimal("0.2")),
    ]
    world.actions = [
        CorporateActionNotice(
            isin=LEADER,
            purpose="DIVIDEND - RS 2 PER SHARE",
            ex_date=SESSION + timedelta(days=7),
            record_date=SESSION + timedelta(days=7),
            knowable_date=CALENDAR[-2],
        ),
        CorporateActionNotice(
            isin=LEADER,
            purpose="DIVIDEND - RS 1 PER SHARE",
            ex_date=CALENDAR[-20],
            record_date=CALENDAR[-20],
            knowable_date=CALENDAR[-25],
        ),
    ]
    return world


def _row(isin: str, close: Decimal) -> UniverseRow:
    return UniverseRow(
        isin=isin,
        close=close,
        return_1w=Decimal("0.01"),
        return_4w=Decimal("0.02"),
        return_13w=Decimal("0.05"),
        return_52w=Decimal("0.30"),
        from_52w_high=Decimal("-0.01"),
        volatility_20=Decimal("0.004"),
        median_traded_value=Decimal(20_000_000),
        cap_tier="mid",
        size_rank=150,
        sector=SECTOR.get(isin),
        asm=False,
        delivery_pct=Decimal("40"),
        revenue_ttm_growth=None,
        pat_ttm_growth=None,
        roe=None,
        pe_ttm=None,
        sector_median_pe=None,
        pe_vs_sector=None,
        filing_date=None,
        announcements_5s=0,
    )


def sheets_for(world: ScreenWorld, *, breadth: str | None = "0.6") -> CommonsSheets:
    session = world.calendar[-1]
    closes = {b.isin: b.raw_close for b in world.bars if b.trade_date == session}
    rows = tuple(_row(isin, closes[isin]) for isin in sorted(closes))
    market = MarketSheet(
        trading_date=session,
        index_trends=(),
        breadth=None
        if breadth is None
        else BreadthReading(
            universe=len(rows),
            measured=len(rows),
            above_mean_50=0,
            above_mean_share=Decimal(breadth),
            advancers=0,
            decliners=0,
            unchanged=0,
        ),
        india_vix=None,
        sector_returns=(),
        delivery_anomalies=None,
        policy_rate=None,
    )
    parameters: dict[str, Any] = {"universe": PARAMS.document()}
    m, u, b = CommonsSheets.digests(
        trading_date=session,
        sheet_version=SHEET_VERSION,
        parameters=parameters,
        market=market,
        universe=rows,
        gaps=(),
    )
    return CommonsSheets(
        trading_date=session,
        sheet_version=SHEET_VERSION,
        parameters=parameters,
        market=market,
        universe=rows,
        gaps=(),
        market_digest=m,
        universe_digest=u,
        build_digest=b,
        built_at=clock().now(),
    )
