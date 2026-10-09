"""M17.1 — the Research Commons market and universe sheets (pre-registration §2, §4 step 2).

What these tests pin, against the acceptance criteria:

1. **PIT guard.** A future-dated record in any input trips `PitError`: a price bar, an adjusted
   close, a filing, an index level published after the session, a macro reading, a surveillance
   entry, a classification row, an announcement, or a session. Every case builds the same world
   with one record added, so the guard is what fails, not the fixture.
2. **Gaps and refusals.** Each optional source, made unavailable, is named in ``gaps``; the build
   completes and the field it fed is ``None``. A red interlock, an unreadable interlock, a session
   after the clock's date, a session with no bars, or an unavailable price source refuses the
   build.
3. **Determinism.** Two builds of the same world give byte-identical canonical sheets and the same
   ``build_digest``. That holds with a different clock, with input order reversed, and under a
   caller's low-precision decimal context. Changing one close changes the digest.

Also pinned, as inversion guards: returns use adjusted closes (a 2:1 split is not a -50 % year),
the floor keeps the liquid names, an excluded list removes a name while ASM only flags it, and
breadth, delivery anomalies and TTM growth all point the right way. The lake adapter is exercised
on a scratch lake: it reads NSE only, takes the latest macro revision, never picks an L0 snapshot
dated after the session, and reports an absent dataset as unavailable.

Offline: the scratch lake lives under ``tmp_path``; nothing touches the network or the real lake.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analyst.commons import (
    CommonsRefusedError,
    CommonsSheets,
    InMemoryCommonsStore,
    LakeCommonsSource,
    SourceUnavailableError,
    UniverseParameters,
    UniverseRow,
    build_commons_sheets,
)
from analyst.commons.sheets import (
    INDIA_VIX_SERIES,
    POLICY_RATE_SERIES,
    SECTOR_INDEX_SERIES,
    AdjustedClose,
    AnnouncementRecord,
    EquityBar,
    FilingFact,
    IndexLevel,
    MacroReading,
    SectorAssignment,
    SurveillanceEntry,
    canonical_bytes,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.macro.models import Frequency, MacroFact, MacroRelease, Unit
from dataplatform.ingest.models import is_isin_check_digit_valid
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.query import Dataset, PitError
from dataplatform.store.l0 import L0Store
from dataplatform.store.macro_series import write_release
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

SESSION = date(2026, 10, 8)
FUTURE = SESSION + timedelta(days=1)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse_market_structure" / "2026-09-08"
PARAMS = UniverseParameters(
    series="EQ",
    min_median_traded_value_inr=Decimal(10_000_000),
    median_lookback_sessions=20,
    excluded_surveillance=("GSM", "ESM"),
    flagged_surveillance=("ASM",),
)


def _isin(n: int) -> str:
    """A shape-valid ``INE`` ISIN with a correct ISO 6166 check digit."""
    body = f"INE{n:06d}01"
    digits = "".join(str(int(ch, 36)) for ch in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    isin = body + str((10 - total % 10) % 10)
    assert is_isin_check_digit_valid(isin)
    return isin


RISING, FALLING, ILLIQUID, GSM_NAME, ASM_NAME, DARK, SPLIT = (_isin(n) for n in range(1, 8))


def _weekdays(end: date, count: int) -> list[date]:
    out: list[date] = []
    day = end
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return out[::-1]


CALENDAR = _weekdays(SESSION, 270)


# ── a synthetic world and a CommonsSource over it ────────────────────────────────────────────────


@dataclass
class World:
    """Everything the fake source serves. It serves it unfiltered, so a leak reaches the guard."""

    calendar: list[date]
    bars: list[EquityBar] = field(default_factory=list)
    adjusted: list[AdjustedClose] = field(default_factory=list)
    levels: list[IndexLevel] = field(default_factory=list)
    macro: list[MacroReading] = field(default_factory=list)
    surveillance: dict[str, list[SurveillanceEntry]] = field(default_factory=dict)
    sectors: list[SectorAssignment] = field(default_factory=list)
    filings: list[FilingFact] = field(default_factory=list)
    announcements: list[AnnouncementRecord] = field(default_factory=list)


class FakeSource:
    def __init__(
        self, world: World, *, missing: frozenset[str] = frozenset(), reverse: bool = False
    ) -> None:
        self.world = world
        self.missing = missing
        self.reverse = reverse

    def _serve[R](
        self, name: str, records: Sequence[R], knowable: Callable[[R], date]
    ) -> Dataset[R]:
        if name in self.missing:
            raise SourceUnavailableError(name, "not built in this fixture")
        ordered = list(reversed(records)) if self.reverse else list(records)
        return Dataset.declaring(name, ordered, knowable_date=knowable)

    def sessions(self, through: date, count: int) -> Dataset[date]:
        return self._serve("sessions", self.world.calendar[-count:], lambda d: d)

    def equity_bars(self, sessions: Sequence[date], series: str) -> Dataset[EquityBar]:
        return self._serve("equity_bars", self.world.bars, lambda b: b.trade_date)

    def adjusted_closes(
        self, isins: frozenset[str], sessions: Sequence[date]
    ) -> Dataset[AdjustedClose]:
        return self._serve("adjusted_closes", self.world.adjusted, lambda r: r.trade_date)

    def index_levels(self, series_ids: Sequence[str], through: date) -> Dataset[IndexLevel]:
        return self._serve("index_levels", self.world.levels, lambda r: r.knowable_date)

    def macro_readings(self, series_ids: Sequence[str], through: date) -> Dataset[MacroReading]:
        return self._serve("macro_readings", self.world.macro, lambda r: r.knowable_date)

    def surveillance(self, stage: str, through: date) -> Dataset[SurveillanceEntry]:
        return self._serve(
            f"surveillance:{stage}",
            self.world.surveillance.get(stage, []),
            lambda e: e.knowable_date,
        )

    def sectors(self, through: date) -> Dataset[SectorAssignment]:
        return self._serve("sectors", self.world.sectors, lambda r: r.knowable_date)

    def filings(self, through: date) -> Dataset[FilingFact]:
        return self._serve("filings", self.world.filings, lambda f: f.filing_date)

    def announcements(self, start: date, through: date) -> Dataset[AnnouncementRecord]:
        return self._serve("announcements", self.world.announcements, lambda r: r.knowable_date)


def _bar(isin: str, day: date, close: Decimal, qty: int, deliv: int | None = None) -> EquityBar:
    return EquityBar(
        isin=isin,
        trade_date=day,
        close=close,
        traded_qty=qty,
        traded_value=(close * qty).quantize(Decimal("0.0001")),
        deliv_qty=deliv,
        deliv_pct=Decimal("45.5") if deliv is not None else None,
    )


def _quarter_ends(last: date, count: int) -> list[date]:
    ends = [last]
    while len(ends) < count:
        prev = ends[-1].replace(day=1) - timedelta(days=1)
        prev = (prev.replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1)
        ends.append(prev)
    return ends[::-1]


def _facts(isin: str, *, revenue: Sequence[str], pat: Sequence[str]) -> list[FilingFact]:
    out: list[FilingFact] = []
    for i, end in enumerate(_quarter_ends(date(2026, 6, 30), len(pat))):
        start = (end - timedelta(days=85)).replace(day=1)
        filed = end + timedelta(days=40)
        for concept, value in (
            ("revenue_from_operations", revenue[i]),
            ("profit_after_tax", pat[i]),
            ("shares_outstanding", "1000000"),
        ):
            out.append(
                FilingFact(
                    isin,
                    start,
                    end,
                    filed,
                    f"{isin}-{i}",
                    Nature.CONSOLIDATED,
                    concept,
                    None,
                    Decimal(value),
                )
            )
    out.append(
        FilingFact(
            isin,
            date(2025, 4, 1),
            date(2026, 3, 31),
            date(2026, 5, 15),
            f"{isin}-fy",
            Nature.CONSOLIDATED,
            "shareholders_equity_excl_revaluation",
            None,
            Decimal("40000000"),
        )
    )
    return out


def _world() -> World:
    cal = CALENDAR
    world = World(calendar=list(cal))
    split_at = 200
    for i, day in enumerate(cal):
        e_close = Decimal(150) + (Decimal(1) if i % 2 else Decimal(-1))
        world.bars += [
            _bar(RISING, day, Decimal(100) + Decimal("0.5") * i, 200_000, 20_000),
            _bar(FALLING, day, Decimal(300) - Decimal("0.5") * i, 100_000, 10_000),
            _bar(ILLIQUID, day, Decimal(50), 1_000, 100),
            _bar(GSM_NAME, day, Decimal(200), 100_000, 10_000),
            _bar(ASM_NAME, day, e_close, 100_000, 50_000 if day == SESSION else 10_000),
        ]
        if day != SESSION:
            world.bars.append(_bar(DARK, day, Decimal(500), 100_000))
        raw_split = Decimal(400) if i < split_at else Decimal(200)
        world.bars.append(_bar(SPLIT, day, raw_split, 100_000 if i < split_at else 200_000))
        for bar in world.bars[-7:]:
            close = bar.close
            if bar.isin == SPLIT and i < split_at:
                close = bar.close / 2  # back-adjusted for the 2:1 split
            world.adjusted.append(AdjustedClose(bar.isin, day, close))
        for series_id, base in (
            ("IN.NSE.NIFTY_50.CLOSE", 20000),
            ("IN.NSE.NIFTY_500.CLOSE", 18000),
            ("IN.NSE.NIFTY_IT.CLOSE", 30000),
        ):
            world.levels.append(IndexLevel(series_id, day, Decimal(base + 10 * i), day))
    world.macro = [
        MacroReading(INDIA_VIX_SERIES, SESSION, Decimal("14.5"), SESSION),
        MacroReading(INDIA_VIX_SERIES, cal[-2], Decimal("13.9"), cal[-2]),
        MacroReading(POLICY_RATE_SERIES, date(2026, 9, 1), Decimal("5.5"), date(2026, 9, 1)),
    ]
    listed = cal[-1]
    world.surveillance = {
        "ASM": [SurveillanceEntry(ASM_NAME, "ASM", listed)],
        "GSM": [SurveillanceEntry(GSM_NAME, "GSM", listed)],
        "ESM": [],
    }
    world.sectors = [
        SectorAssignment(RISING, "Information Technology", date(2026, 10, 1)),
        SectorAssignment(FALLING, "Information Technology", date(2026, 10, 1)),
        SectorAssignment(ASM_NAME, "Power", date(2026, 10, 1)),
    ]
    world.filings = _facts(
        RISING,
        revenue=["100", "100", "100", "100", "150", "150", "150", "150"],
        pat=["10", "10", "10", "10", "20", "20", "20", "20"],
    ) + _facts(FALLING, revenue=["300"] * 4, pat=["30"] * 4)
    world.announcements = [
        AnnouncementRecord(RISING, "a1", cal[-1]),
        AnnouncementRecord(RISING, "a1", cal[-1]),  # the same disclosure seen twice: counted once
        AnnouncementRecord(RISING, "a2", cal[-4]),
        AnnouncementRecord(RISING, "old", cal[-7]),  # before the 5-session window
    ]
    return world


class Gate:
    def __init__(self, green: bool = True, reason: str = "green") -> None:
        self.green = green
        self.reason = reason

    def __call__(self, trading_date: date) -> Gate:
        return self

    def __bool__(self) -> bool:
        return self.green


def _clock(hour: int = 21, day: date = SESSION) -> FrozenClock:
    return FrozenClock(datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST))


def _build(
    world: World | None = None,
    *,
    missing: frozenset[str] = frozenset(),
    gate: Gate | None = None,
    clock: FrozenClock | None = None,
    reverse: bool = False,
) -> CommonsSheets:
    return build_commons_sheets(
        SESSION,
        source=FakeSource(world or _world(), missing=missing, reverse=reverse),
        gate=gate if gate is not None else Gate(),
        clock=clock if clock is not None else _clock(),
        universe=PARAMS,
    )


@pytest.fixture(scope="module")
def sheets() -> CommonsSheets:
    return _build()


def _row(sheets: CommonsSheets, isin: str) -> UniverseRow:
    (row,) = [r for r in sheets.universe if r.isin == isin]
    return row


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


# ── the universe sheet ───────────────────────────────────────────────────────────────────────────


def test_the_universe_is_the_preregistered_screen_keyed_and_sorted_by_isin(
    sheets: CommonsSheets,
) -> None:
    isins = [r.isin for r in sheets.universe]
    # ILLIQUID is under the floor, GSM_NAME is excluded, and DARK did not print on the session.
    assert isins == sorted([RISING, FALLING, ASM_NAME, SPLIT])
    assert ILLIQUID not in isins and GSM_NAME not in isins and DARK not in isins


def test_asm_flags_and_never_excludes(sheets: CommonsSheets) -> None:
    assert _row(sheets, ASM_NAME).asm is True
    assert _row(sheets, RISING).asm is False


def test_returns_are_struck_on_adjusted_closes(sheets: CommonsSheets) -> None:
    rising = _row(sheets, RISING)
    n = len(CALENDAR) - 1
    close = Decimal(100) + Decimal("0.5") * n

    def then(back: int) -> Decimal:
        return Decimal(100) + Decimal("0.5") * (n - back)

    assert rising.return_1w == _q(close / then(5) - 1)
    assert rising.return_4w == _q(close / then(20) - 1)
    assert rising.return_13w == _q(close / then(65) - 1)
    assert rising.return_52w == _q(close / then(260) - 1)
    assert rising.from_52w_high == Decimal("0.000000")  # a rising name sits at its high
    falling = _row(sheets, FALLING)
    assert falling.return_52w is not None and falling.return_52w < 0
    assert falling.from_52w_high is not None and falling.from_52w_high < 0
    # The 2:1 split is in the window. On raw closes the 52-week return would read -50 %.
    split = _row(sheets, SPLIT)
    assert split.return_52w == Decimal("0.000000")
    assert split.close == Decimal("200.0000")  # the sheet's close is the exchange's, unadjusted


def test_volatility_median_value_and_size_rank(sheets: CommonsSheets) -> None:
    # The ASM name alternates 149/151 every session; the steady risers have near-zero volatility.
    assert _row(sheets, ASM_NAME).volatility_20 is not None
    asm_vol = _row(sheets, ASM_NAME).volatility_20
    rising_vol = _row(sheets, RISING).volatility_20
    assert asm_vol is not None and rising_vol is not None and asm_vol > rising_vol
    assert _row(sheets, FALLING).median_traded_value >= PARAMS.min_median_traded_value_inr
    ranks = {r.isin: r.size_rank for r in sheets.universe}
    # Median close x quantity over 126 sessions, descending. RISING's median close in that window
    # is ~203 on 200,000 shares (~4.06 cr), just ahead of SPLIT's flat 4 cr. The excluded GSM
    # name is still ranked, at 2 cr, ahead of FALLING (~1.97 cr) and ASM_NAME (1.5 cr).
    assert ranks[RISING] == 1 and ranks[SPLIT] == 2
    assert ranks[FALLING] == 4 and ranks[ASM_NAME] == 5
    assert _row(sheets, SPLIT).cap_tier == "large"


def test_fundamentals_are_pit_and_point_the_right_way(sheets: CommonsSheets) -> None:
    rising = _row(sheets, RISING)
    assert rising.pat_ttm_growth == Decimal("1.000000")  # 80 against 40
    assert rising.revenue_ttm_growth == Decimal("0.500000")  # 600 against 400
    assert rising.roe == Decimal("0.000002")  # 80 / 40,000,000
    assert rising.filing_date == date(2026, 8, 9)
    falling = _row(sheets, FALLING)
    assert falling.pat_ttm_growth is None  # four quarters cannot give a TTM growth
    # P/E against the sector median: both IT names have one, and the median sits between them.
    assert rising.pe_ttm is not None and falling.pe_ttm is not None
    assert rising.sector_median_pe == falling.sector_median_pe
    assert rising.pe_vs_sector is not None and falling.pe_vs_sector is not None
    assert (rising.pe_vs_sector > 1) == (rising.pe_ttm > falling.pe_ttm)
    assert _row(sheets, SPLIT).sector is None and _row(sheets, SPLIT).pe_vs_sector is None


def test_announcements_count_distinct_disclosures_in_the_last_five_sessions(
    sheets: CommonsSheets,
) -> None:
    assert _row(sheets, RISING).announcements_5s == 2
    assert _row(sheets, FALLING).announcements_5s == 0


# ── the market sheet ─────────────────────────────────────────────────────────────────────────────


def test_index_trends_breadth_vix_sectors_delivery_and_rate(sheets: CommonsSheets) -> None:
    market = sheets.market
    nifty = {t.series_id: t for t in market.index_trends}["IN.NSE.NIFTY_50.CLOSE"]
    assert nifty.session == SESSION
    assert nifty.vs_mean_50 is not None and nifty.vs_mean_50 > 0  # a rising index is above
    assert nifty.vs_mean_200 is not None and nifty.vs_mean_200 > nifty.vs_mean_50
    assert market.breadth is not None
    assert market.breadth.universe == 4
    # RISING is above its mean; FALLING is below; ASM_NAME closes at 151 against a 150 mean.
    assert market.breadth.above_mean_50 == 2
    assert market.breadth.advancers == 2 and market.breadth.decliners == 1
    assert market.india_vix is not None and market.india_vix.value == Decimal("14.5000")
    (it,) = market.sector_returns
    assert it.series_id == "IN.NSE.NIFTY_IT.CLOSE"
    assert it.return_1m is not None and it.return_6m is not None and it.return_6m > it.return_1m
    assert market.delivery_anomalies is not None
    assert [a.isin for a in market.delivery_anomalies.top] == [ASM_NAME]
    assert market.delivery_anomalies.top[0].ratio == Decimal("5.000000")
    assert market.policy_rate is not None and market.policy_rate.value == Decimal("5.5000")
    unpublished = {g.source for g in sheets.gaps}
    assert set(SECTOR_INDEX_SERIES) - {"IN.NSE.NIFTY_IT.CLOSE"} <= unpublished


# ── acceptance 1: a future-dated input trips the PIT guard ───────────────────────────────────────


def _leak(kind: str) -> Callable[[World], None]:
    def add(world: World) -> None:
        if kind == "price":
            world.bars.append(_bar(RISING, FUTURE, Decimal(999), 200_000))
        elif kind == "adjusted":
            world.adjusted.append(AdjustedClose(RISING, FUTURE, Decimal(999)))
        elif kind == "filing":
            world.filings.append(replace(world.filings[0], filing_date=FUTURE, filing_id="late"))
        elif kind == "index":
            # The session's own level, but published the next day: knowable after the session.
            level = world.levels[-1]
            world.levels[-1] = replace(level, knowable_date=FUTURE)
        elif kind == "macro":
            world.macro.append(MacroReading(INDIA_VIX_SERIES, SESSION, Decimal(30), FUTURE))
        elif kind == "surveillance":
            world.surveillance["ASM"].append(SurveillanceEntry(RISING, "ASM", FUTURE))
        elif kind == "sector":
            world.sectors.append(SectorAssignment(SPLIT, "Power", FUTURE))
        elif kind == "announcement":
            world.announcements.append(AnnouncementRecord(RISING, "tomorrow", FUTURE))
        elif kind == "session":
            world.calendar.append(FUTURE)
        else:  # pragma: no cover - a typo in the parametrize list
            raise AssertionError(kind)

    return add


@pytest.mark.parametrize(
    "kind",
    [
        "price",
        "adjusted",
        "filing",
        "index",
        "macro",
        "surveillance",
        "sector",
        "announcement",
        "session",
    ],
)
def test_a_future_dated_input_trips_the_pit_guard(kind: str) -> None:
    world = _world()
    _leak(kind)(world)
    with pytest.raises(PitError, match=r"not yet knowable|not knowable"):
        _build(world)


def test_a_record_knowable_on_the_session_itself_is_admitted(sheets: CommonsSheets) -> None:
    """The boundary is inclusive. The session's own VIX and bars are in the sheet."""
    assert sheets.market.india_vix is not None
    assert sheets.market.india_vix.observed == SESSION


# ── acceptance 2: a missing source is a gap; a red day is a refusal ──────────────────────────────


@pytest.mark.parametrize(
    ("missing", "gap", "check"),
    [
        (
            "adjusted_closes",
            "adjusted_closes",
            lambda s: s.market.breadth is None and _row(s, RISING).return_1w is None,
        ),
        ("index_levels", "index_levels", lambda s: s.market.index_trends == ()),
        (
            "macro_readings",
            "india_vix",
            lambda s: s.market.india_vix is None and s.market.policy_rate is None,
        ),
        (
            "surveillance:GSM",
            "surveillance:GSM",
            lambda s: GSM_NAME in {r.isin for r in s.universe},
        ),
        ("surveillance:ASM", "surveillance:ASM", lambda s: _row(s, ASM_NAME).asm is None),
        (
            "sectors",
            "sectors",
            lambda s: _row(s, RISING).sector is None and _row(s, RISING).pe_vs_sector is None,
        ),
        (
            "filings",
            "filings",
            lambda s: _row(s, RISING).pe_ttm is None and _row(s, RISING).filing_date is None,
        ),
        ("announcements", "announcements", lambda s: _row(s, RISING).announcements_5s is None),
    ],
)
def test_a_missing_source_is_named_in_gaps_and_the_build_completes(
    missing: str, gap: str, check: Callable[[CommonsSheets], bool]
) -> None:
    built = _build(missing=frozenset({missing}))
    assert gap in {g.source for g in built.gaps}
    assert check(built)
    built.verify()


def test_an_unremovable_excluded_list_is_said_so_in_gaps() -> None:
    built = _build(missing=frozenset({"surveillance:ESM"}))
    reasons = {g.source: g.reason for g in built.gaps}
    assert "could not be removed" in reasons["surveillance"]


def test_a_red_data_day_refuses_to_build() -> None:
    with pytest.raises(CommonsRefusedError, match="nse_eod not PUBLISHED"):
        _build(gate=Gate(green=False, reason="nse_eod not PUBLISHED"))


def test_an_unreadable_interlock_is_never_green() -> None:
    class Down:
        def __call__(self, trading_date: date) -> Gate:
            raise ConnectionError("status database unreachable")

    with pytest.raises(ConnectionError):
        build_commons_sheets(
            SESSION, source=FakeSource(_world()), gate=Down(), clock=_clock(), universe=PARAMS
        )


def test_a_session_after_the_clock_is_refused() -> None:
    with pytest.raises(CommonsRefusedError, match="after today"):
        _build(clock=_clock(day=SESSION - timedelta(days=1)))


def test_a_session_with_no_bars_is_refused() -> None:
    world = _world()
    world.bars = [b for b in world.bars if b.trade_date != SESSION]
    with pytest.raises(CommonsRefusedError, match="no EQ bar"):
        _build(world)


@pytest.mark.parametrize("missing", ["sessions", "equity_bars"])
def test_prices_are_the_spine_and_never_a_gap(missing: str) -> None:
    with pytest.raises(CommonsRefusedError):
        _build(missing=frozenset({missing}))


# ── acceptance 3: the same lake builds a byte-identical digest ───────────────────────────────────


def _sheet_bytes(sheets: CommonsSheets) -> bytes:
    return canonical_bytes(sheets.model_dump(mode="json", exclude={"built_at"}))


def test_the_same_lake_builds_a_byte_identical_digest_twice(sheets: CommonsSheets) -> None:
    again = _build(clock=_clock(hour=23), reverse=True)
    assert again.build_digest == sheets.build_digest
    assert _sheet_bytes(again) == _sheet_bytes(sheets)
    assert again.built_at != sheets.built_at
    world = _world()  # built at default precision; only the build runs in the caller's context
    with localcontext() as ctx:
        ctx.prec = 5  # a caller's context must not move a digit
        assert _build(world).build_digest == sheets.build_digest


def test_a_changed_input_changes_the_digest(sheets: CommonsSheets) -> None:
    world = _world()
    world.bars = [
        replace(b, close=b.close + Decimal("0.01"))
        if (b.isin, b.trade_date) == (FALLING, SESSION)
        else b
        for b in world.bars
    ]
    changed = _build(world)
    assert changed.build_digest != sheets.build_digest
    assert changed.market_digest == sheets.market_digest  # the raw close feeds the universe only


# ── the in-memory store: append-only by digest ───────────────────────────────────────────────────


def test_the_store_records_a_build_once_and_never_rewrites_it(sheets: CommonsSheets) -> None:
    store = InMemoryCommonsStore()
    at = datetime(2026, 10, 8, 21, 5, tzinfo=IST)
    assert store.record(sheets, recorded_at=at) is True
    assert store.record(_build(clock=_clock(hour=22)), recorded_at=at) is False
    assert store.digests(SESSION) == (sheets.build_digest,)
    world = _world()
    world.macro = world.macro[1:]
    later = _build(world)
    assert store.record(later, recorded_at=at + timedelta(hours=1)) is True
    assert store.digests(SESSION) == (sheets.build_digest, later.build_digest)
    latest = store.latest(SESSION)
    assert latest is not None and latest.build_digest == later.build_digest


def test_a_build_that_does_not_reproduce_its_digest_is_refused(sheets: CommonsSheets) -> None:
    tampered = sheets.model_copy(update={"universe": sheets.universe[1:]})
    with pytest.raises(ValueError, match="does not reproduce"):
        InMemoryCommonsStore().record(tampered, recorded_at=datetime(2026, 10, 8, tzinfo=IST))


def test_the_universe_parameters_refuse_a_float_floor() -> None:
    with pytest.raises(TypeError):
        UniverseParameters("EQ", 1e7, 20, ("GSM",), ("ASM",))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="excluded and flagged"):
        UniverseParameters("EQ", Decimal(1), 20, ("ASM",), ("ASM",))


# ── the lake adapter, on a scratch lake ──────────────────────────────────────────────────────────

_LAKE_SESSIONS = _weekdays(SESSION, 25)


def _write_prices(root: Path) -> None:
    def record(
        isin: str, exchange: str, series: str, day: date, close: Decimal
    ) -> dict[str, object]:
        price = close.quantize(Decimal("0.0001"))
        return {
            "isin": isin,
            "exchange": exchange,
            "symbol": isin[3:9],
            "series": series,
            "trade_date": day,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "last": price,
            "prev_close": price,
            "total_traded_qty": 100_000,
            "total_traded_value": (price * 100_000).quantize(Decimal("0.0001")),
            "total_trades": 1_000,
            "deliv_qty": 40_000,
            "deliv_pct": Decimal("40.0000"),
        }

    for i, day in enumerate(_LAKE_SESSIONS):
        rows = [
            record(RISING, "NSE", "EQ", day, Decimal(100 + i)),
            record(FALLING, "NSE", "EQ", day, Decimal(300 - i)),
            record(RISING, "BSE", "A", day, Decimal(1)),  # another venue: never read
            record(ILLIQUID, "NSE", "BE", day, Decimal(1000)),  # another series: never read
        ]
        path = l1_partition_path(PRICES_RAW_DATASET, day, data_root=root)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=PRICES_RAW_SCHEMA), path)


def _vix(day: date, released: date, value: str, seq: int = 0) -> MacroRelease:
    fact = MacroFact(
        series_id=INDIA_VIX_SERIES,
        period_start=None,
        period_end=day,
        release_date=released,
        frequency=Frequency.DAILY,
        unit=Unit.INDEX,
        value=Decimal(value),
        revision_seq=seq,
        source="nifty_india_vix_history",
    )
    return MacroRelease(release_date=released, source="nifty_india_vix_history", facts=(fact,))


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    _write_prices(tmp_path)
    write_release(_vix(SESSION, SESSION, "14.0"), data_root=tmp_path)
    write_release(_vix(SESSION, SESSION, "14.2", seq=1), data_root=tmp_path)  # same-day revision
    write_release(_vix(FUTURE, FUTURE, "99.0"), data_root=tmp_path)  # released after the session
    l0 = L0Store(clock=_clock(), data_root=tmp_path)
    listed = SESSION - timedelta(days=1)
    for source, name in (("nse_asm_list", "reportASM"), ("nse_gsm_list", "reportGSM")):
        payload = (FIXTURES / f"{name}.json").read_bytes()
        l0.put(source, listed, f"{name}_{listed:%Y%m%d}.json", payload)
    l0.put("nse_gsm_list", FUTURE, f"reportGSM_{FUTURE:%Y%m%d}.json", b"[]")
    csv = (FIXTURES / "ind_niftytotalmarket_list.csv").read_bytes()
    l0.put(
        "nse_industry_classification", listed, f"ind_niftytotalmarket_list_{listed:%Y%m%d}.csv", csv
    )
    return tmp_path


def test_the_lake_adapter_reads_nse_only_in_the_series_asked(lake: Path) -> None:
    with LakeCommonsSource(clock=_clock(), data_root=lake) as source:
        sessions = source.sessions(SESSION, 10).records
        assert list(sessions) == _LAKE_SESSIONS[-10:]
        bars = source.equity_bars(_LAKE_SESSIONS[-3:], "EQ").records
        assert {b.isin for b in bars} == {RISING, FALLING}
        assert {b.trade_date for b in bars} == set(_LAKE_SESSIONS[-3:])
        adjusted = source.adjusted_closes(frozenset({RISING}), _LAKE_SESSIONS[-2:]).records
        # No L2 partition: the raw close is the adjusted close, and nothing else is served.
        assert [(a.isin, a.close) for a in adjusted] == [
            (RISING, Decimal("123.0000")),
            (RISING, Decimal("124.0000")),
        ]


def test_the_lake_adapter_takes_the_latest_revision_and_nothing_released_later(
    lake: Path,
) -> None:
    with LakeCommonsSource(clock=_clock(), data_root=lake) as source:
        readings = source.macro_readings((INDIA_VIX_SERIES,), SESSION).records
        assert [(r.period_end, r.value) for r in readings] == [(SESSION, Decimal("14.200000"))]


def test_the_lake_adapter_never_picks_a_snapshot_dated_after_the_session(lake: Path) -> None:
    listed = SESSION - timedelta(days=1)
    expected = {row["isin"] for row in json.loads((FIXTURES / "reportGSM.json").read_bytes())}
    with LakeCommonsSource(clock=_clock(), data_root=lake) as source:
        gsm = source.surveillance("GSM", SESSION).records
        assert {e.isin for e in gsm} == expected
        assert {e.knowable_date for e in gsm} == {listed}
        asm = source.surveillance("ASM", SESSION).records
        assert asm and all(e.stage == "ASM" for e in asm)
        sectors = source.sectors(SESSION).records
        assert len(sectors) > 700 and {s.knowable_date for s in sectors} == {listed}
        with pytest.raises(SourceUnavailableError, match="nse_esm_list"):
            source.surveillance("ESM", SESSION)
        with pytest.raises(SourceUnavailableError, match="pit_fundamentals"):
            source.filings(SESSION)
        with pytest.raises(SourceUnavailableError, match="announcements"):
            source.announcements(SESSION - timedelta(days=7), SESSION)


def test_a_build_on_the_scratch_lake_completes_with_its_gaps_named(lake: Path) -> None:
    with LakeCommonsSource(clock=_clock(), data_root=lake) as source:
        built = build_commons_sheets(
            SESSION, source=source, gate=Gate(), clock=_clock(), universe=PARAMS
        )
        again = build_commons_sheets(
            SESSION, source=source, gate=Gate(), clock=_clock(hour=23), universe=PARAMS
        )
    assert [r.isin for r in built.universe] == sorted([RISING, FALLING])
    gaps = {g.source for g in built.gaps}
    assert {"surveillance:ESM", "surveillance", "pit_fundamentals", "announcements"} <= gaps
    assert built.market.india_vix is not None
    assert built.market.india_vix.value == Decimal("14.2000")
    assert again.build_digest == built.build_digest
