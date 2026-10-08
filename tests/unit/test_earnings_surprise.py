"""M16.3: the earnings-surprise (PEAD) leg of the M10.7 swing composite (arm A5), off by default.

The definition is fixed by the task, not fitted: SUE = (latest quarter's EPS - the same quarter's a
year earlier) / the sample stdev of the eight year-over-year differences before it, on standalone
quarterly figures restated to the latest share count, active for 63 sessions from the filing date.
"""

from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, NamedTuple

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.policies.earnings_surprise import (
    ACTIVE_SESSIONS,
    M10_7_EARNINGS_SURPRISE,
    EarningsSurprisePanel,
    standardised_unexpected_earnings,
)
from backtest.policies.swing_composite import (
    SwingCompositeParameters,
    SwingRecord,
    composite_scores,
)
from backtest.run import BacktestError, BacktestResult, open_swing_lake, run_swing_composite
from backtest.sweep import ARMS, EARNINGS_SURPRISE_ARM
from dataplatform.ingest.indices import TRI_METHOD_PUBLISHED, TriPoint, TriSeries, write_tri_l1
from dataplatform.ingest.xbrl.models import Filing, FundamentalFact, Nature, Taxonomy
from dataplatform.query.pit import PitError
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.pit_fundamentals import write_pit
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

_Q: Final = Decimal("0.0001")


# ── an offline lake: five names, a published index, and a decade of standalone filings ──────────


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


CALENDAR: Final = _weekdays(date(2012, 1, 2), 320)
START: Final = CALENDAR[260]

#: Five names whose price legs differ, so the M10.7 composite has an ordering to trade on.
LAKE_NAMES: Final = (
    "INE001A01011",
    "INE002A01019",
    "INE003A01017",
    "INE004A01015",
    "INE005A01012",
)
#: The two that file results in the lake: one beats its own history hard, one misses it.
BEATER: Final = LAKE_NAMES[3]
MISSER: Final = LAKE_NAMES[0]


def _close(n: int, i: int) -> Decimal:
    return Decimal(100 + 20 * n) * (Decimal(1) + Decimal(n + 1) / Decimal(2000)) ** i


def _price_row(isin: str, n: int, i: int, session: date) -> dict[str, object]:
    price, volume = _close(n, i).quantize(_Q), 100_000 * (n + 1)
    deliv = Decimal(30 + 10 * ((n * 7 + i // 20) % 5))
    return {
        "isin": isin,
        "exchange": "NSE",
        "symbol": isin[:6],
        "series": "EQ",
        "trade_date": session,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "last": price,
        "prev_close": price,
        "total_traded_qty": volume,
        "total_traded_value": (price * volume).quantize(_Q),
        "total_trades": volume,
        "deliv_qty": int(volume * deliv / 100),
        "deliv_pct": deliv,
    }


def _quarter_ends(first: date, count: int) -> list[date]:
    ends: list[date] = []
    year, month = first.year, first.month
    while len(ends) < count:
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        ends.append(nxt - timedelta(days=1))
        month += 3
        if month > 12:
            year, month = year + 1, month - 12
    return ends


def _quarter_start(period_end: date) -> date:
    """The first day of the quarter ending on the month-end ``period_end``."""
    return (date(period_end.year, period_end.month, 1) - timedelta(days=40)).replace(day=1)


def quarterly_filing(
    isin: str,
    period_end: date,
    *,
    eps: str,
    shares: str | None = "1000000",
    filed: date | None = None,
    filing_id: str | None = None,
    nature: Nature = Nature.STANDALONE,
    face_value: str = "10",
) -> Filing:
    """One quarterly results filing with EPS, face value and the parser's derived share count."""
    start = _quarter_start(period_end)
    filed = filed or period_end + timedelta(days=45)
    fid = filing_id or f"{isin}-{period_end.isoformat()}-{nature.value}"
    values = {
        "revenue_from_operations": ("1000", False),
        "eps_basic": (eps, False),
        "face_value_per_share": (face_value, False),
        **({"shares_outstanding": (shares, True)} if shares is not None else {}),
    }
    facts = tuple(
        FundamentalFact(
            isin=isin,
            period_start=start,
            period_end=period_end,
            filing_date=filed,
            nature=nature,
            taxonomy=Taxonomy.IND_AS,
            filing_id=fid,
            concept=concept,
            segment=None,
            value=Decimal(value),
            derived=derived,
            source="nse_filings",
            l0_key=None,
        )
        for concept, (value, derived) in values.items()
    )
    return Filing(
        isin=isin,
        symbol=isin[:6],
        taxonomy=Taxonomy.IND_AS,
        name="Fixture Co",
        period_start=start,
        period_end=period_end,
        filing_date=filed,
        nature=nature,
        filing_id=fid,
        source="nse_filings",
        facts=facts,
    )


#: Sixteen quarter-ends, 2009-03-31 .. 2012-12-31; the last is filed inside the replay window.
LAKE_QUARTERS: Final = _quarter_ends(date(2009, 3, 1), 16)
#: A steady history whose year-over-year differences vary, so the stdev is a real number.
_HISTORY: Final = ("10", "11", "10", "12", "11", "12", "11", "13", "12", "14", "12", "13", "13")
LAST_FILED: Final = CALENDAR[275]


def build_lake(root: Path) -> Path:
    for i, session in enumerate(CALENDAR):
        rows = [_price_row(isin, n, i, session) for n, isin in enumerate(LAKE_NAMES)]
        path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=root)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=PRICES_RAW_SCHEMA), path)
    points = tuple(
        TriPoint(
            index_slug="nifty50",
            index_name="Nifty 50",
            as_of=session,
            tri_value=Decimal(10_000 + i),
            method=TRI_METHOD_PUBLISHED,
        )
        for i, session in enumerate(CALENDAR)
    )
    write_tri_l1(
        TriSeries(
            index_slug="nifty50", index_name="Nifty 50", method=TRI_METHOD_PUBLISHED, points=points
        ),
        data_root=root,
    )
    for isin, last in ((BEATER, "40"), (MISSER, "-20")):
        history = [*(_HISTORY * 2)[: len(LAKE_QUARTERS) - 1], last]
        for quarter, eps in zip(LAKE_QUARTERS, history, strict=True):
            filed = LAST_FILED if quarter == LAKE_QUARTERS[-1] else None
            write_pit(quarterly_filing(isin, quarter, eps=eps, filed=filed), data_root=root)
    return root


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    return build_lake(tmp_path)


_SMOKE: Final = SwingCompositeParameters(top_n=2, sell_band=3)


def _run(root: Path, params: SwingCompositeParameters = _SMOKE) -> BacktestResult:
    return run_swing_composite(
        start=START, end=CALENDAR[-1], parameters=params, data_root=root, adjusted=False
    )


def _nav_sha(run: BacktestResult) -> str:
    text = "\n".join(f"{d.isoformat()} {v}" for d, v in run.nav_path)
    return hashlib.sha256(text.encode()).hexdigest()


# ── M10.7 is byte-identical: the digest pinned from the pre-change code (main at 523882e) ────────

M10_7_REPLAY_DIGEST: Final = "52ea26824042e81157de243a63e8a5b70a853cc7517ea53326fd0091483cfa77"
M10_7_NAV_SHA: Final = "5dbd3e1f480d188831bd43c99be2c4eb97b2c3c102b9c187660c3910305e16ab"


def test_m10_7_replays_byte_identical_to_the_pre_change_code(lake: Path) -> None:
    """The earnings leg at weight 0 moves nothing: journal, book and NAV path as main produced."""
    run = _run(lake)
    assert run.result.digest() == M10_7_REPLAY_DIGEST
    assert _nav_sha(run) == M10_7_NAV_SHA


def test_the_default_parameters_repr_is_unchanged_and_the_preset_adds_one_leg() -> None:
    """The run ledger keys on ``repr(parameters)``: M10.7's must not mention the new leg at zero."""
    assert "earnings_surprise" not in repr(SwingCompositeParameters())
    assert "weight_earnings_surprise=Decimal('1')" in repr(M10_7_EARNINGS_SURPRISE)
    default = SwingCompositeParameters()
    changed = {
        f.name
        for f in fields(default)
        if getattr(default, f.name) != getattr(M10_7_EARNINGS_SURPRISE, f.name)
    }
    # Equal weight with the three M10.7 legs, and nothing else moved.
    assert changed == {"weight_earnings_surprise"}
    assert M10_7_EARNINGS_SURPRISE.weight_earnings_surprise == default.weight_high
    assert EARNINGS_SURPRISE_ARM.swing == M10_7_EARNINGS_SURPRISE
    assert EARNINGS_SURPRISE_ARM not in ARMS  # no campaign manifest moves


# ── the definition, on in-memory facts ──────────────────────────────────────────────────────────


class _Fact(NamedTuple):
    isin: str
    period_start: date | None
    period_end: date
    filing_date: date
    filing_id: str
    nature: Nature
    concept: str
    segment: str | None
    value: Decimal


ISIN: Final = "INE100A01010"
QUARTERS: Final = _quarter_ends(date(2015, 3, 1), 13)
#: e0..e12 with YoY differences d4..d11 = 1, 2, 0, 1, 1, 0, 1, 2: mean 1, sample variance 4/7.
EPS: Final = ("10", "10", "10", "10", "11", "12", "10", "11", "12", "12", "11", "13")
#: The latest quarter at 15 is a +3 year-over-year change against e8 = 12.
BEAT_SUE: Final = (Decimal(3) / (Decimal(4) / Decimal(7)).sqrt()).quantize(Decimal("0.00000001"))


def _filed(period_end: date) -> date:
    return period_end + timedelta(days=45)


def quarter_facts(
    period_end: date,
    eps: str,
    *,
    shares: str | None = "1000000",
    filed: date | None = None,
    filing_id: str | None = None,
    nature: Nature = Nature.STANDALONE,
    isin: str = ISIN,
) -> list[_Fact]:
    start = _quarter_start(period_end)
    filed = filed or _filed(period_end)
    fid = filing_id or f"{isin}-{period_end.isoformat()}-{nature.value}"
    out = [_Fact(isin, start, period_end, filed, fid, nature, "eps_basic", None, Decimal(eps))]
    if shares is not None:
        out += [
            _Fact(isin, start, period_end, filed, fid, nature, c, None, Decimal(v))
            for c, v in (("shares_outstanding", shares), ("paid_up_equity_capital", "10000000"))
        ]
    return out


def series(latest: str, *, eps: tuple[str, ...] = EPS, **per_quarter: object) -> list[_Fact]:
    facts: list[_Fact] = []
    for quarter, value in zip(QUARTERS, (*eps, latest), strict=True):
        facts += quarter_facts(quarter, value, **per_quarter)  # type: ignore[arg-type]
    return facts


AS_OF: Final = _filed(QUARTERS[-1])


def test_a_beat_scores_positive_and_a_miss_negative() -> None:
    """Fails if the sign is inverted: the latest quarter up on its own history is a positive SUE."""
    beat = standardised_unexpected_earnings(series("15"), as_of=AS_OF)[ISIN]
    miss = standardised_unexpected_earnings(series("9"), as_of=AS_OF)[ISIN]
    assert beat.sue == BEAT_SUE
    assert beat.sue > 0
    assert miss.sue == -BEAT_SUE
    assert beat.filing_date == AS_OF
    assert beat.latest_period_end == QUARTERS[-1]


def test_the_composite_ranks_the_beat_above_the_miss() -> None:
    """Fails if the leg is wired with its sign inverted: at positive weight the beat ranks first."""

    def record(isin: str, surprise: Decimal | None) -> SwingRecord:
        return SwingRecord(
            isin=isin,
            high_proximity=Decimal("0.9"),
            delivery_share=Decimal("0.5"),
            momentum_12_1=Decimal("0.1"),
            volatility=Decimal("0.02"),
            price=Decimal(100),
            knowable_date=AS_OF,
            earnings_surprise=surprise,
        )

    records = [record("INE1", BEAT_SUE), record("INE2", -BEAT_SUE), record("INE3", None)]
    scores = composite_scores(records, M10_7_EARNINGS_SURPRISE)
    assert scores["INE1"] > scores["INE3"] > scores["INE2"]
    # At weight 0 (M10.7) the leg moves nothing, whatever the records carry.
    assert len(set(composite_scores(records, SwingCompositeParameters()).values())) == 1


def test_a_future_filing_is_refused() -> None:
    """PIT: a fact filed after the as-of date is a named leak, never a silent drop."""
    with pytest.raises(PitError, match="invariant #7"):
        standardised_unexpected_earnings(series("15"), as_of=AS_OF - timedelta(days=1))


def test_the_panel_never_reads_a_filing_before_its_date() -> None:
    """The day before the latest filing, the reading is the previous quarter's, not the new one."""
    later = quarter_facts(date(2018, 6, 30), "40")  # the quarter after QUARTERS[-1]
    calendar = _weekdays(date(2015, 1, 1), 1200)
    panel = EarningsSurprisePanel([*series("15"), *later], calendar)
    filed = later[0].filing_date
    before = max(day for day in calendar if day < filed)
    on_or_after = min(day for day in calendar if day >= filed)
    reading = panel.reading(ISIN, before)
    assert reading is not None and reading.sue == BEAT_SUE
    after = panel.reading(ISIN, on_or_after)
    assert after is not None and after.latest_period_end == later[0].period_end


def test_the_leg_is_on_for_63_sessions_then_zero() -> None:
    """The filing session is session 1; session 63 still carries the SUE, session 64 carries 0."""
    calendar = _weekdays(date(2015, 1, 1), 1200)
    # A Saturday filing: the window opens on the Monday after it.
    saturday = next(d for d in (AS_OF + timedelta(days=k) for k in range(7)) if d.weekday() == 5)
    facts = series("15", filed=None)
    facts = [f._replace(filing_date=saturday) if f.period_end == QUARTERS[-1] else f for f in facts]
    panel = EarningsSurprisePanel(facts, calendar)
    first = calendar.index(saturday + timedelta(days=2))
    assert panel.value(ISIN, calendar[first - 1]) != BEAT_SUE  # Friday: not yet knowable
    assert panel.value(ISIN, calendar[first]) == BEAT_SUE
    assert panel.value(ISIN, calendar[first + ACTIVE_SESSIONS - 1]) == BEAT_SUE
    assert panel.value(ISIN, calendar[first + ACTIVE_SESSIONS]) == Decimal(0)
    assert panel.value(ISIN, calendar[first + 300]) == Decimal(0)
    with pytest.raises(ValueError, match="not a session"):
        panel.value(ISIN, saturday)


def test_insufficient_history_gets_no_signal() -> None:
    """Twelve quarters, a gap, a quarter without a share count, or a flat history: no reading."""
    short = [f for f in series("15") if f.period_end != QUARTERS[0]]
    assert standardised_unexpected_earnings(short, as_of=AS_OF) == {}
    gap = [f for f in series("15") if f.period_end != QUARTERS[6]]
    assert standardised_unexpected_earnings(gap, as_of=AS_OF) == {}
    no_count = [
        f
        for f in series("15")
        if not (f.period_end == QUARTERS[3] and f.concept == "shares_outstanding")
    ]
    assert standardised_unexpected_earnings(no_count, as_of=AS_OF) == {}
    flat = series("15", eps=("10",) * 12)
    assert standardised_unexpected_earnings(flat, as_of=AS_OF) == {}
    calendar = _weekdays(date(2015, 1, 1), 1200)
    assert EarningsSurprisePanel(short, calendar).value(ISIN, calendar[-1]) is None
    assert EarningsSurprisePanel([], calendar).value(ISIN, calendar[-1]) is None


def test_consolidated_figures_are_never_read() -> None:
    """Standalone only: a consolidated series alone is no signal, and never mixes into one."""
    consolidated = series("15", nature=Nature.CONSOLIDATED)
    assert standardised_unexpected_earnings(consolidated, as_of=AS_OF) == {}
    noisy = [f._replace(value=f.value * 3) if f.concept == "eps_basic" else f for f in consolidated]
    both = standardised_unexpected_earnings([*series("15"), *noisy], as_of=AS_OF)
    assert both[ISIN].sue == BEAT_SUE


def test_a_split_or_bonus_mid_history_leaves_the_surprise_unchanged() -> None:
    """EPS is restated to the latest share count, so a 1:5 split or a 1:1 bonus is not a miss.

    Without the restatement the quarter after a split reads as an 80 % collapse in EPS; with it the
    series is the unsplit one and so is its SUE, to the last decimal.
    """
    split_at = 7
    for multiple in (5, 2):  # a 10 -> 2 face-value split; a 1:1 bonus at an unchanged face value
        facts: list[_Fact] = []
        for i, (quarter, eps) in enumerate(zip(QUARTERS, (*EPS, "15"), strict=True)):
            after = i >= split_at
            facts += quarter_facts(
                quarter,
                str(Decimal(eps) / multiple) if after else eps,
                shares=str(1_000_000 * multiple) if after else "1000000",
            )
        reading = standardised_unexpected_earnings(facts, as_of=AS_OF)[ISIN]
        assert reading.sue == BEAT_SUE


def test_a_restatement_wins_only_from_its_own_filing_date() -> None:
    """A restated quarter replaces the original as of its filing, and is invisible before it."""
    restated_on = AS_OF + timedelta(days=30)
    restatement = quarter_facts(QUARTERS[-1], "13", filed=restated_on, filing_id="restated")
    facts = [*series("15"), *restatement]
    with pytest.raises(PitError):
        standardised_unexpected_earnings(facts, as_of=AS_OF)
    original = standardised_unexpected_earnings(series("15"), as_of=AS_OF)[ISIN]
    revised = standardised_unexpected_earnings(facts, as_of=restated_on)[ISIN]
    assert original.sue == BEAT_SUE
    assert revised.sue == (BEAT_SUE / 3).quantize(Decimal("0.00000001"))


# ── end to end on the offline lake ──────────────────────────────────────────────────────────────


def test_the_swing_lake_carries_the_leg_from_the_pit_store(lake: Path) -> None:
    swing = open_swing_lake(
        start=START,
        end=CALENDAR[-1],
        floors=[],
        data_root=lake,
        adjusted=False,
        earnings_surprise=True,
    )
    try:
        decision = CALENDAR[280]
        swing.features.load([decision])
        by_isin = {r.isin: r for r in swing.features.records(decision)}
        assert set(by_isin) == set(LAKE_NAMES)
        beat, miss = by_isin[BEATER].earnings_surprise, by_isin[MISSER].earnings_surprise
        assert beat is not None and beat > 0
        assert miss is not None and miss < 0
        assert all(
            by_isin[i].earnings_surprise is None for i in LAKE_NAMES if i not in {BEATER, MISSER}
        )
    finally:
        swing.close()


def test_a_lake_opened_without_the_leg_carries_none_and_an_a5_run_on_it_fails_loud(
    lake: Path,
) -> None:
    swing = open_swing_lake(
        start=START, end=CALENDAR[-1], floors=[], data_root=lake, adjusted=False
    )
    try:
        swing.features.load([CALENDAR[280]])
        assert all(r.earnings_surprise is None for r in swing.features.records(CALENDAR[280]))
        with pytest.raises(BacktestError, match="earnings surprise"):
            run_swing_composite(
                start=START,
                end=CALENDAR[-1],
                parameters=M10_7_EARNINGS_SURPRISE,
                data_root=lake,
                adjusted=False,
                lake=swing,
            )
    finally:
        swing.close()


def test_a5_smoke_runs_and_differs_from_m10_7(lake: Path) -> None:
    """A tiny A5 replay: deterministic, and its journal is not M10.7's (the leg is in force)."""
    params = replace(_SMOKE, weight_earnings_surprise=Decimal(1))
    first, second = _run(lake, params), _run(lake, params)
    assert first.result.digest() == second.result.digest()
    assert first.result.digest() != M10_7_REPLAY_DIGEST
