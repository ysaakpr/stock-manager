"""M17.2 — the mechanical shortlist (pre-registration §3).

What these tests pin, against the acceptance criteria:

1. **Rank direction, per factor.** For each of the four factors, a universe where only that factor
   differs must put the higher value first, at rank 1, and the lower value last, at rank 0. Invert
   any factor's direction and its case fails. The builder is pinned too: on the M17.1 world the
   rising name beats the falling one on 12-1 momentum, relative strength and the M16.3 surprise.
2. **The rule's edges.** An undefined factor takes the mean rank (1/2), ties average, equal
   composites break by ISIN, and the shortlist stops at 40.
3. **Reuse, not re-derivation.** The earnings-surprise factor equals what
   `EarningsSurprisePanel.value` gives on the same facts and calendar.
4. **PIT and gaps.** A future-dated index level, adjusted close or filing raises `PitError`. A
   missing index becomes a gap, and relative strength takes the mean rank for every name.
5. **Frozen and deterministic.** The rule hash is the sha256 of the rule's source. Two builds of
   the same lake give the same digest whatever the clock. The store re-verifies on read.

Offline: the M17.1 synthetic world, with no lake and no network.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from analyst.commons import (
    SHORTLIST_RULE_HASH,
    SHORTLIST_SIZE,
    CommonsSheets,
    InMemoryShortlistStore,
    Shortlist,
    build_commons_sheets,
    build_shortlist,
)
from analyst.commons.sheets import AdjustedClose, FilingFact, IndexLevel
from analyst.commons.shortlist import (
    MOMENTUM_LONG_SESSIONS,
    MOMENTUM_SHORT_SESSIONS,
    RS_INDEX_SERIES,
    RS_SESSIONS,
    ShortlistFactors,
    log_liquidity,
    momentum_12_1,
    percentile_ranks,
    rank_shortlist,
    relative_strength,
    shortlist_rule_source,
)
from backtest.policies.earnings_surprise import EarningsSurprisePanel
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.xbrl.models import Nature
from dataplatform.query import PitError
from tests.unit.test_commons_sheets import (
    ASM_NAME,
    CALENDAR,
    FALLING,
    FUTURE,
    PARAMS,
    RISING,
    SESSION,
    SPLIT,
    FakeSource,
    Gate,
    World,
    _clock,
    _isin,
    _world,
)

FACTORS = ("momentum_12_1", "relative_strength_20", "earnings_surprise", "log_liquidity")
RANK_FIELD = {
    "momentum_12_1": "rank_momentum_12_1",
    "relative_strength_20": "rank_relative_strength_20",
    "earnings_surprise": "rank_earnings_surprise",
    "log_liquidity": "rank_log_liquidity",
}


# ── the rule, pure ───────────────────────────────────────────────────────────────────────────────


def _neutral(isin: str) -> ShortlistFactors:
    return ShortlistFactors(
        isin=isin,
        momentum_12_1=Decimal("0.1"),
        relative_strength_20=Decimal("0.01"),
        earnings_surprise=Decimal("0.5"),
        log_liquidity=Decimal("17"),
    )


HIGH, MID, LOW = _isin(901), _isin(902), _isin(903)


@pytest.mark.parametrize("factor", FACTORS)
def test_a_higher_factor_ranks_higher_and_lists_first(factor: str) -> None:
    """The inversion guard: flip any factor's direction and this case fails for it."""
    # Names are listed so that ISIN order (the tie-break) is the opposite of the expected order.
    values = {HIGH: Decimal("3"), MID: Decimal("2"), LOW: Decimal("1")}
    ordered = sorted(values, reverse=True)
    factors = [replace(_neutral(isin), **{factor: values[isin]}) for isin in ordered]  # type: ignore[arg-type]
    entries = rank_shortlist(factors)
    assert [e.isin for e in entries] == [HIGH, MID, LOW]
    by_isin = {e.isin: e for e in entries}
    assert getattr(by_isin[HIGH], RANK_FIELD[factor]) == Decimal(1)
    assert getattr(by_isin[MID], RANK_FIELD[factor]) == Decimal("0.5")
    assert getattr(by_isin[LOW], RANK_FIELD[factor]) == Decimal(0)
    assert by_isin[HIGH].composite > by_isin[MID].composite > by_isin[LOW].composite
    for other in FACTORS:
        if other != factor:
            assert {getattr(e, RANK_FIELD[other]) for e in entries} == {Decimal("0.5")}


def test_the_composite_is_the_equal_weight_mean_of_the_four_ranks() -> None:
    a, b = _isin(911), _isin(912)
    entries = rank_shortlist(
        [
            ShortlistFactors(a, Decimal(2), Decimal(1), Decimal(1), Decimal(1)),
            ShortlistFactors(b, Decimal(1), Decimal(2), Decimal(2), Decimal(2)),
        ]
    )
    by_isin = {e.isin: e for e in entries}
    assert by_isin[a].composite == Decimal("0.25")  # (1 + 0 + 0 + 0) / 4
    assert by_isin[b].composite == Decimal("0.75")
    assert [e.isin for e in entries] == [b, a]
    assert [e.position for e in entries] == [1, 2]


def test_an_undefined_factor_takes_the_mean_rank() -> None:
    ranks = percentile_ranks({HIGH: Decimal(5), MID: None, LOW: Decimal(1)})
    assert ranks == {HIGH: Decimal(1), MID: Decimal("0.5"), LOW: Decimal(0)}
    # The mean of the defined ranks is 1/2 (to the 8-dp quantum each rank is rounded to), so an
    # undefined name neither gains nor loses.
    many = percentile_ranks({_isin(i): Decimal(i % 7) for i in range(1, 30)})
    assert abs(sum(many.values(), Decimal(0)) / len(many) - Decimal("0.5")) <= Decimal("0.00000001")
    assert percentile_ranks({HIGH: None, LOW: None}) == {HIGH: Decimal("0.5"), LOW: Decimal("0.5")}
    assert percentile_ranks({HIGH: Decimal(3)}) == {HIGH: Decimal("0.5")}


def test_ties_take_the_average_rank() -> None:
    ranks = percentile_ranks(
        {_isin(1): Decimal(1), _isin(2): Decimal(2), _isin(3): Decimal(2), _isin(4): Decimal(3)}
    )
    assert ranks[_isin(1)] == Decimal(0)
    assert ranks[_isin(2)] == ranks[_isin(3)] == Decimal("0.5")
    assert ranks[_isin(4)] == Decimal(1)


def test_equal_composites_break_by_isin() -> None:
    names = [_isin(n) for n in (40, 10, 30, 20)]
    entries = rank_shortlist([_neutral(isin) for isin in names])
    assert [e.isin for e in entries] == sorted(names)


def test_the_shortlist_stops_at_forty() -> None:
    assert SHORTLIST_SIZE == 40
    universe = [
        replace(_neutral(_isin(n)), momentum_12_1=Decimal(n)) for n in range(1, SHORTLIST_SIZE + 11)
    ]
    entries = rank_shortlist(universe)
    assert len(entries) == SHORTLIST_SIZE
    assert [e.position for e in entries] == list(range(1, SHORTLIST_SIZE + 1))
    # The ten weakest on momentum are the ones left out.
    assert {e.isin for e in entries} == {_isin(n) for n in range(11, SHORTLIST_SIZE + 11)}


def test_a_duplicate_name_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        rank_shortlist([_neutral(HIGH), _neutral(HIGH)])


def test_factor_definitions_point_the_right_way() -> None:
    assert momentum_12_1(Decimal(100), Decimal(150)) == Decimal("0.5")  # t-12m 100 -> t-1m 150
    down = momentum_12_1(Decimal(150), Decimal(100))
    assert down is not None and down < 0
    assert momentum_12_1(None, Decimal(1)) is None and momentum_12_1(Decimal(0), Decimal(1)) is None
    # Up 10 % against an index up 5 %: ahead by 1.10 / 1.05 - 1.
    assert relative_strength(Decimal("0.10"), Decimal("0.05")) == Decimal("0.04761905")
    behind = relative_strength(Decimal("0.01"), Decimal("0.05"))
    assert behind is not None and behind < 0
    assert relative_strength(Decimal("0.01"), None) is None
    big, small = log_liquidity(Decimal(10**9)), log_liquidity(Decimal(10**7))
    assert big is not None and small is not None and big > small
    assert log_liquidity(Decimal(0)) is None


# ── the builder over the M17.1 world ─────────────────────────────────────────────────────────────


def _quarter_ends(last: date, count: int) -> list[date]:
    """``count`` consecutive month-end quarter-ends ending at ``last``, ascending."""
    ends = [last]
    while len(ends) < count:
        first = ends[-1].replace(day=1)
        for _ in range(2):
            first = (first - timedelta(days=1)).replace(day=1)
        ends.append(first - timedelta(days=1))
    return ends[::-1]


def _eps_facts(isin: str, eps: list[str]) -> list[FilingFact]:
    """Standalone quarterly EPS and share count, one filing per quarter, 45 days after it."""
    facts: list[FilingFact] = []
    for end, value in zip(_quarter_ends(date(2026, 6, 30), len(eps)), eps, strict=True):
        start = (end.replace(day=1) - timedelta(days=40)).replace(day=1)
        filed, fid = end + timedelta(days=45), f"{isin}-{end.isoformat()}-sa"
        for concept, v in (("eps_basic", value), ("shares_outstanding", "1000000")):
            facts.append(
                FilingFact(
                    isin, start, end, filed, fid, Nature.STANDALONE, concept, None, Decimal(v)
                )
            )
    return facts


#: Thirteen quarters with a varying history; the last one beats it hard, or misses it hard.
_HISTORY = ["10", "11", "10", "12", "11", "12", "11", "13", "12", "14", "12", "13"]
BEAT = [*_HISTORY, "25"]
MISS = [*_HISTORY, "3"]


def _shortlist_world() -> World:
    world = _world()
    world.filings += _eps_facts(RISING, BEAT) + _eps_facts(FALLING, MISS)
    return world


def _sheets(world: World) -> CommonsSheets:
    return build_commons_sheets(
        SESSION, source=FakeSource(world), gate=Gate(), clock=_clock(), universe=PARAMS
    )


@pytest.fixture(scope="module")
def world() -> World:
    return _shortlist_world()


@pytest.fixture(scope="module")
def sheets(world: World) -> CommonsSheets:
    return _sheets(world)


@pytest.fixture(scope="module")
def shortlist(world: World, sheets: CommonsSheets) -> Shortlist:
    return build_shortlist(sheets, source=FakeSource(world), clock=_clock(22))


def _entry(shortlist: Shortlist, isin: str):  # type: ignore[no-untyped-def]
    (entry,) = [e for e in shortlist.entries if e.isin == isin]
    return entry


def test_the_shortlist_covers_the_sheet_universe(
    sheets: CommonsSheets, shortlist: Shortlist
) -> None:
    assert {e.isin for e in shortlist.entries} == {r.isin for r in sheets.universe}
    assert shortlist.universe_size == len(sheets.universe) == 4
    assert shortlist.build_digest == sheets.build_digest
    assert shortlist.rule_hash == SHORTLIST_RULE_HASH
    assert shortlist.gaps == ()


def test_momentum_is_the_t252_to_t21_adjusted_return(shortlist: Shortlist) -> None:
    n = len(CALENDAR) - 1

    def close(i: int) -> Decimal:
        return Decimal(100) + Decimal("0.5") * i

    expected = close(n - MOMENTUM_SHORT_SESSIONS) / close(n - MOMENTUM_LONG_SESSIONS) - 1
    assert _entry(shortlist, RISING).momentum_12_1 == expected.quantize(Decimal("0.00000001"))
    falling = _entry(shortlist, FALLING).momentum_12_1
    assert falling is not None and falling < 0
    # SPLIT halves at a 2:1 split inside the window; adjusted, it is flat, not -50 %.
    assert _entry(shortlist, SPLIT).momentum_12_1 == Decimal(0)
    assert _entry(shortlist, RISING).rank_momentum_12_1 == Decimal(1)
    assert _entry(shortlist, FALLING).rank_momentum_12_1 == Decimal(0)


def test_relative_strength_is_against_nifty_500(
    sheets: CommonsSheets, shortlist: Shortlist
) -> None:
    n = len(CALENDAR) - 1
    index_return = Decimal(18000 + 10 * n) / Decimal(18000 + 10 * (n - RS_SESSIONS)) - 1
    (row,) = [r for r in sheets.universe if r.isin == RISING]
    assert row.return_4w is not None
    expected = ((1 + row.return_4w) / (1 + index_return) - 1).quantize(Decimal("0.00000001"))
    assert _entry(shortlist, RISING).relative_strength_20 == expected
    falling = _entry(shortlist, FALLING).relative_strength_20
    assert falling is not None and falling < 0
    assert _entry(shortlist, RISING).rank_relative_strength_20 == Decimal(1)
    assert _entry(shortlist, FALLING).rank_relative_strength_20 == Decimal(0)


def test_earnings_surprise_is_the_m16_3_leg(world: World, shortlist: Shortlist) -> None:
    panel = EarningsSurprisePanel(world.filings, CALENDAR[-MOMENTUM_LONG_SESSIONS - 1 :])
    for isin in (RISING, FALLING, ASM_NAME, SPLIT):
        assert _entry(shortlist, isin).earnings_surprise == panel.value(isin, SESSION)
    beat, miss = _entry(shortlist, RISING), _entry(shortlist, FALLING)
    assert beat.earnings_surprise is not None and beat.earnings_surprise > 0
    assert miss.earnings_surprise is not None and miss.earnings_surprise < 0
    assert beat.rank_earnings_surprise == Decimal(1)
    assert miss.rank_earnings_surprise == Decimal(0)
    # The two names without thirteen quarters take the mean rank.
    assert _entry(shortlist, SPLIT).earnings_surprise is None
    assert _entry(shortlist, SPLIT).rank_earnings_surprise == Decimal("0.5")
    assert shortlist.coverage["earnings_surprise"] == 2


def test_liquidity_is_the_log_median_traded_value(
    sheets: CommonsSheets, shortlist: Shortlist
) -> None:
    for row in sheets.universe:
        assert _entry(shortlist, row.isin).log_liquidity == log_liquidity(row.median_traded_value)
    most = max(sheets.universe, key=lambda r: r.median_traded_value)
    assert _entry(shortlist, most.isin).rank_log_liquidity == Decimal(1)


def test_the_rising_name_heads_the_shortlist(shortlist: Shortlist) -> None:
    assert shortlist.entries[0].isin == RISING
    assert shortlist.entries[-1].isin == FALLING


@pytest.mark.parametrize("leak", ["index_level", "adjusted_close", "filing"])
def test_a_future_record_trips_the_pit_guard(sheets: CommonsSheets, leak: str) -> None:
    world = _shortlist_world()
    if leak == "index_level":
        world.levels.append(IndexLevel(RS_INDEX_SERIES, SESSION, Decimal(1), FUTURE))
    elif leak == "adjusted_close":
        world.adjusted.append(AdjustedClose(RISING, FUTURE, Decimal(1)))
    else:
        world.filings.append(replace(world.filings[-1], filing_date=FUTURE, filing_id="late"))
    with pytest.raises(PitError):
        build_shortlist(sheets, source=FakeSource(world), clock=_clock(22))


def test_a_missing_index_is_a_gap_and_relative_strength_takes_the_mean(
    world: World, sheets: CommonsSheets
) -> None:
    shortlist = build_shortlist(
        sheets, source=FakeSource(world, missing=frozenset({"index_levels"})), clock=_clock(22)
    )
    assert [g.source for g in shortlist.gaps] == ["index_levels"]
    assert shortlist.coverage["relative_strength_20"] == 0
    assert {e.rank_relative_strength_20 for e in shortlist.entries} == {Decimal("0.5")}
    assert {e.relative_strength_20 for e in shortlist.entries} == {None}


def test_a_lagging_index_is_named_and_leaves_the_order_unchanged(
    sheets: CommonsSheets, shortlist: Shortlist
) -> None:
    lagging = _shortlist_world()
    lag_from = CALENDAR[-3]
    lagging.levels = [
        lv
        for lv in lagging.levels
        if not (lv.series_id == RS_INDEX_SERIES and lv.session > lag_from)
    ]
    late = build_shortlist(sheets, source=FakeSource(lagging), clock=_clock(22))
    (gap,) = late.gaps
    assert gap.source == RS_INDEX_SERIES and lag_from.isoformat() in gap.reason
    assert late.coverage["relative_strength_20"] == 4
    # The index return is common to every name, so the ranks and the order do not move.
    assert [e.isin for e in late.entries] == [e.isin for e in shortlist.entries]
    assert [e.rank_relative_strength_20 for e in late.entries] == [
        e.rank_relative_strength_20 for e in shortlist.entries
    ]
    assert late.entries[0].relative_strength_20 != shortlist.entries[0].relative_strength_20


def test_a_stale_index_is_not_used(sheets: CommonsSheets) -> None:
    stale = _shortlist_world()
    cutoff = SESSION - timedelta(days=10)
    stale.levels = [
        lv for lv in stale.levels if not (lv.series_id == RS_INDEX_SERIES and lv.session > cutoff)
    ]
    shortlist = build_shortlist(sheets, source=FakeSource(stale), clock=_clock(22))
    assert [g.source for g in shortlist.gaps] == [RS_INDEX_SERIES]
    assert {e.relative_strength_20 for e in shortlist.entries} == {None}


def test_missing_filings_are_a_gap(world: World, sheets: CommonsSheets) -> None:
    shortlist = build_shortlist(
        sheets, source=FakeSource(world, missing=frozenset({"filings"})), clock=_clock(22)
    )
    assert "filings" in {g.source for g in shortlist.gaps}
    assert {e.rank_earnings_surprise for e in shortlist.entries} == {Decimal("0.5")}


def test_a_short_calendar_is_a_gap_not_a_guess(world: World) -> None:
    short = replace(world, calendar=world.calendar[-200:])
    sheets = _sheets(short)
    shortlist = build_shortlist(sheets, source=FakeSource(short), clock=_clock(22))
    assert "momentum_12_1" in {g.source for g in shortlist.gaps}
    assert {e.momentum_12_1 for e in shortlist.entries} == {None}


# ── frozen, deterministic, stored ────────────────────────────────────────────────────────────────


def test_the_rule_hash_is_the_sha256_of_its_source() -> None:
    source = shortlist_rule_source()
    assert hashlib.sha256(source).hexdigest() == SHORTLIST_RULE_HASH
    assert len(SHORTLIST_RULE_HASH) == 64
    # It covers the rule and the M16.3 definition it reuses.
    assert b"def rank_shortlist" in source and b"def _surprise_for" in source


def test_the_rule_hash_feeds_mandate_hash() -> None:
    from analyst.fundmanager import load_roster, mandate_hash

    roster = load_roster()
    book = roster.books[0]
    ours = mandate_hash(book, b"", b"", SHORTLIST_RULE_HASH, roster.rails)
    other = mandate_hash(book, b"", b"", "0" * 64, roster.rails)
    assert ours != other


def test_the_same_lake_gives_the_same_shortlist(
    world: World, sheets: CommonsSheets, shortlist: Shortlist
) -> None:
    again = build_shortlist(
        sheets,
        source=FakeSource(world, reverse=True),
        clock=FrozenClock(datetime(2026, 10, 9, 6, 0, tzinfo=IST)),
    )
    assert again.shortlist_digest == shortlist.shortlist_digest
    assert again.entries == shortlist.entries
    shortlist.verify()


def test_a_changed_close_changes_the_digest(world: World, shortlist: Shortlist) -> None:
    changed = _shortlist_world()
    t_12 = CALENDAR[-1 - MOMENTUM_LONG_SESSIONS]
    changed.adjusted = [
        AdjustedClose(a.isin, a.trade_date, a.close * 2)
        if (a.isin, a.trade_date) == (FALLING, t_12)
        else a
        for a in changed.adjusted
    ]
    other = build_shortlist(_sheets(changed), source=FakeSource(changed), clock=_clock(22))
    assert other.shortlist_digest != shortlist.shortlist_digest


def test_a_tampered_shortlist_does_not_verify(shortlist: Shortlist) -> None:
    forged = shortlist.model_copy(update={"entries": tuple(reversed(shortlist.entries))})
    with pytest.raises(ValueError, match="does not reproduce"):
        forged.verify()


def test_the_in_memory_store_is_append_only(shortlist: Shortlist) -> None:
    store = InMemoryShortlistStore()
    at = datetime(2026, 10, 8, 22, 0, tzinfo=IST)
    assert store.record(shortlist, recorded_at=at) is True
    assert store.record(shortlist, recorded_at=at) is False
    assert store.latest(SESSION) == shortlist
    assert store.latest(SESSION - timedelta(days=1)) is None
