"""M17.9 — the frozen base-rate table (study §3).

What these tests pin, against the acceptance criteria:

1. **It rebuilds to the same digest.** Two builds of the same synthetic lake, under different
   clocks, give one digest; the file written for it reloads and re-verifies; a tampered file or a
   different expected digest is refused. The committed fixture is the table this lake built when
   the rules were frozen, so a changed rule (or a changed screen) cannot reproduce it.
2. **Cells with n < 30 are thin.** At 29 a cell is thin and at 30 it is not; an empty cell is
   present with ``n = 0``. Every cell of the product exists.
3. **The statistics point the right way.** P(beat) counts positive excess, the excess is the
   name's return minus NIFTY 500's (not the reverse), quantiles are numpy's linear rule.
4. **PIT and the universe.** A bar or a filing dated after the range's end trips the guard; a
   screen's window that does not end on its session raises; a name below the ₹1 cr floor is never
   observed.

Offline: a synthetic lake (`tests.unit.commons_screen_world`), no network.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from analyst.commons.base_rates import (
    ALL,
    HORIZONS,
    REGIME_KEYS,
    SCREEN_KEYS,
    THIN_N,
    TIER_KEYS,
    BaseRateTable,
    build_base_rate_table,
    cell_stats,
    load_frozen,
    load_table,
    observe_session,
    sample_sessions,
    table_dir,
    write_table,
)
from analyst.commons.features import align
from analyst.commons.regime import RegimeIndex
from analyst.commons.sheets import EquityBar
from backtest.policies.earnings_surprise import EarningsSurprisePanel
from dataplatform.query import PitError
from tests.unit.commons_screen_world import (
    FakeScreenSource,
    ScreenWorld,
    breakout,
    clock,
    index_levels,
    pbar,
    raw_of,
    shares_fact,
    trend,
)
from tests.unit.test_commons_sheets import _isin, _weekdays

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "commons" / "base_rates" / "synthetic.json"
)
END = date(2025, 6, 30)
CAL = _weekdays(END, 420)
START = CAL[270]
TRENDS = tuple(_isin(n) for n in range(900, 910))
#: Quiet names, so the universe (52) is larger than the composite (40) and NONE has members.
QUIET = tuple(_isin(n) for n in range(920, 960))
ILLIQUID = _isin(911)
POPPER = _isin(912)


def _world() -> ScreenWorld:
    world = ScreenWorld(calendar=list(CAL))
    for k, isin in enumerate(TRENDS):
        world.bars += trend(isin, f"1.00{k}", amp=f"0.00{k % 4 + 2}", calendar=CAL)
    world.bars += breakout(POPPER, calendar=CAL)
    for k, isin in enumerate(QUIET):
        world.bars += trend(
            isin, "0.9995" if k % 2 else "1.0001", amp=f"0.0{k % 9 + 1}", calendar=CAL
        )
    world.bars += [pbar(ILLIQUID, d, Decimal(50), volume=1_000) for d in CAL]
    world.levels = index_levels(CAL)
    world.filings = [shares_fact(TRENDS[0], CAL[100])]
    return world


def _build(world: ScreenWorld | None = None, hour: int = 22) -> BaseRateTable:
    return build_base_rate_table(
        FakeScreenSource(world or _world()), start=START, end=END, clock=clock(END, hour)
    )


@pytest.fixture(scope="module")
def table() -> BaseRateTable:
    return _build()


# ── statistics ───────────────────────────────────────────────────────────────────────────────────


def test_cell_statistics_are_exact() -> None:
    cell = cell_stats(
        "S1", "large", "RISK_ON", 20, [Decimal(x) for x in ("-0.1", "0", "0.1", "0.2")]
    )
    assert cell.n == 4 and cell.p_beat == Decimal("0.5")  # 0 is not a beat
    assert cell.median_excess == Decimal("0.05")
    assert cell.q25_excess == Decimal("-0.025") and cell.q75_excess == Decimal("0.125")
    assert cell.iqr_excess == Decimal("0.15")
    assert cell_stats("S1", "large", "RISK_ON", 20, [Decimal("0.01")] * 5).p_beat == 1
    assert cell_stats("S1", "large", "RISK_ON", 20, [Decimal("-0.01")] * 5).p_beat == 0


def test_a_cell_under_30_is_thin_and_an_empty_one_is_present() -> None:
    assert THIN_N == 30
    assert cell_stats("S2", "mid", "NEUTRAL", 5, [Decimal("0.01")] * 29).thin
    assert not cell_stats("S2", "mid", "NEUTRAL", 5, [Decimal("0.01")] * 30).thin
    empty = cell_stats("S2", "mid", "NEUTRAL", 5, [])
    assert empty.n == 0 and empty.thin and empty.p_beat is None


# ── the build ────────────────────────────────────────────────────────────────────────────────────


def test_every_cell_of_the_product_is_present_and_thin_is_n_below_30(table: BaseRateTable) -> None:
    assert len(table.cells) == len(SCREEN_KEYS) * (len(TIER_KEYS) + 1) * (
        len(REGIME_KEYS) + 1
    ) * len(HORIZONS)
    assert all(c.thin == (c.n < THIN_N) for c in table.cells)
    pooled = table.cell("NONE", ALL, ALL, 5)
    assert pooled.n >= THIN_N and not pooled.thin
    assert table.cell("S4", ALL, ALL, 20).n == 0  # no SUE history in this lake
    with pytest.raises(KeyError):
        table.cell("S9", ALL, ALL, 5)


def test_pooled_cells_hold_their_parts(table: BaseRateTable) -> None:
    for screen in SCREEN_KEYS:
        for h in HORIZONS:
            parts = sum(table.cell(screen, t, ALL, h).n for t in TIER_KEYS)
            assert table.cell(screen, ALL, ALL, h).n == parts


def test_the_same_lake_rebuilds_the_same_digest(table: BaseRateTable, tmp_path: Path) -> None:
    again = _build(hour=23)
    assert again.digest == table.digest and again.built_at != table.built_at
    path = write_table(table, tmp_path)
    assert path.name.endswith(f"_{table.digest[:12]}.json")
    assert load_table(path, expected_digest=table.digest).digest == table.digest
    assert write_table(again, tmp_path) == path  # the same table is the same file
    with pytest.raises(ValueError, match="not"):
        load_table(path, expected_digest="0" * 64)
    document = json.loads(path.read_text())
    document["cells"][0]["n"] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="reproduce"):
        load_table(path)


def test_only_a_pinned_table_is_served(table: BaseRateTable, tmp_path: Path) -> None:
    with pytest.raises(LookupError, match="frozen"):
        load_frozen(tmp_path, digest=None)
    write_table(table, table_dir(tmp_path))
    assert load_frozen(tmp_path, digest=table.digest).digest == table.digest
    with pytest.raises(LookupError):
        load_frozen(tmp_path, digest="f" * 64)


def test_the_frozen_fixture_is_what_this_lake_builds(table: BaseRateTable) -> None:
    frozen = load_table(FIXTURE)
    assert frozen.digest == table.digest


def test_a_changed_lake_changes_the_digest(table: BaseRateTable) -> None:
    world = _world()
    world.levels = index_levels(CAL, growth="1.001")
    assert _build(world).digest != table.digest


def test_sampling_is_every_fifth_session_with_the_horizon_inside_the_range() -> None:
    positions = sample_sessions(CAL, start=START, end=END, every=5, horizon=60)
    assert positions[0] == CAL.index(START)
    assert all(b - a == 5 for a, b in pairwise(positions))
    assert positions[-1] + 60 <= len(CAL) - 1 < positions[-1] + 65


def test_a_name_below_the_floor_is_never_observed(table: BaseRateTable) -> None:
    world = _world()
    observed = _observations(world)
    assert observed and ILLIQUID not in {o.isin for o in observed}
    assert {o.isin for o in observed} <= {*TRENDS, *QUIET, POPPER}


def _observations(world: ScreenWorld, i: int | None = None) -> list:  # type: ignore[type-arg]
    raw: dict[str, dict[date, EquityBar]] = {}
    for bar in world.bars:
        raw.setdefault(bar.isin, {})[bar.trade_date] = raw_of(bar)
    aligned = align(world.bars, CAL)
    levels = sorted((lv.session, lv.close) for lv in world.levels)
    return observe_session(
        CAL.index(START) if i is None else i,
        calendar=CAL,
        raw=raw,
        aligned=aligned,
        sectors={},
        panel=EarningsSurprisePanel([], CAL),
        regime_index=RegimeIndex(levels),
        levels=levels,
        floor_inr=Decimal(10_000_000),
    )


def test_the_excess_is_the_names_return_minus_the_index(table: BaseRateTable) -> None:
    world = _world()
    i = CAL.index(START)
    observed = {o.isin: o for o in _observations(world)}
    fast = TRENDS[-1]
    bars = {b.trade_date: b for b in world.bars if b.isin == fast}
    level = {lv.session: lv.close for lv in world.levels}
    for h, value in observed[fast].excess:
        stock = bars[CAL[i + h]].close / bars[CAL[i]].close - 1
        index = level[CAL[i + h]] / level[CAL[i]] - 1
        assert value == stock - index
    assert dict(observed[fast].excess)[60] > 0  # it grows 0.9 %/session against 0.05 %


def test_a_raised_floor_observes_fewer_names() -> None:
    world = _world()
    raw: dict[str, dict[date, EquityBar]] = {}
    for bar in world.bars:
        raw.setdefault(bar.isin, {})[bar.trade_date] = raw_of(bar)
    aligned = align(world.bars, CAL)
    levels = sorted((lv.session, lv.close) for lv in world.levels)
    common = {
        "calendar": CAL,
        "raw": raw,
        "aligned": aligned,
        "sectors": {},
        "panel": EarningsSurprisePanel([], CAL),
        "regime_index": RegimeIndex(levels),
        "levels": levels,
    }
    i = CAL.index(START)
    low = observe_session(i, floor_inr=Decimal(10_000_000), **common)  # type: ignore[arg-type]
    high = observe_session(i, floor_inr=Decimal(10**12), **common)  # type: ignore[arg-type]
    assert len(high) < len(low) and high == []


@pytest.mark.parametrize("kind", ["bar", "filing"])
def test_a_record_after_the_range_trips_the_pit_guard(kind: str) -> None:
    world = _world()
    later = date(2025, 7, 1)
    if kind == "bar":
        world.bars.append(pbar(TRENDS[0], later, Decimal(999)))
    else:
        world.filings.append(replace(world.filings[0], filing_date=later))
    with pytest.raises(PitError):
        _build(world)


def test_a_window_that_does_not_end_on_its_session_raises() -> None:
    world = _world()
    i = CAL.index(START)
    raw: dict[str, dict[date, EquityBar]] = {}
    for bar in world.bars:
        raw.setdefault(bar.isin, {})[bar.trade_date] = raw_of(bar)
    aligned = {k: list(v) for k, v in align(world.bars, CAL).items()}
    slot = aligned[TRENDS[0]][i]
    assert slot is not None
    aligned[TRENDS[0]][i] = replace(slot, trade_date=CAL[i + 1])  # a later bar in today's slot
    levels = sorted((lv.session, lv.close) for lv in world.levels)
    with pytest.raises(PitError):
        observe_session(
            i,
            calendar=CAL,
            raw=raw,
            aligned=aligned,
            sectors={},
            panel=EarningsSurprisePanel([], CAL),
            regime_index=RegimeIndex(levels),
            levels=levels,
            floor_inr=Decimal(10_000_000),
        )
