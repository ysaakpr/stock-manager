"""M16.2 — momentum v2's point-in-time industry-momentum gate (off by default).

The gate ranks NSE sectoral indices by their 6-1 month return from *published* levels, keeps the top
five, and drops every classified name whose industry is not mapped to one of them. A name the
classification does not name is gate-neutral and passes (PR #97 review, B1: the 2026 snapshot
omits every name that died before it). The tests here pin, each with an inverted twin where the
rule has a direction: the ranking keeps the strongest indices; the return is 6-1, not 1-6 or 0-6;
unclassified names pass while unmapped and non-top-5 names do not; a level published after the
decision date is never read, and a reading dated after it trips the PIT guard; an index that starts
mid-window is rankable only 180 calendar days after its first published level; the reviewed table
covers the classification exactly; and with the gate off D13's replay digest and run fingerprint
are the ones struck before M16.2.

Fixtures are local on purpose: this file imports no private helper from another test module.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from analyst.journal.models import Decision
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.policies.industry_gate import (
    TOP_K,
    IndexMomentum,
    IndustryGateOutcome,
    apply_industry_gate,
    rank_indices,
)
from backtest.policies.momentum_v2 import (
    D13_INDUSTRY_GATE,
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.rails import (
    BacktestRailPolicy,
    RailGate,
    SectorMap,
    ratified_backtest_rail_policy,
    ratified_sector_map,
)
from backtest.replay import ReplayEngine, ReplayResult, SessionContext, SessionDecision
from backtest.run import _AccountingBroker, backtest_spec
from backtest.run_ledger import run_digest
from backtest.sector_indices import (
    MAX_STALE_DAYS,
    SECTOR_INDEX_MAP_PATH,
    SectorIndexError,
    SectorIndexLevel,
    SectorIndexLevels,
    UnclassifiedShare,
    first_rankable_dates,
    load_sector_index_map,
    unclassified_shares,
)
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import NoReferenceBarError, ReferenceBar, SimBroker
from tests.rails_support import marks_from

D13 = PAPER_RATIFIED_2026_09_06
SESSION = date(2024, 7, 1)
NAMES = tuple(f"INE{index:03d}A01010" for index in range(1, 11))
PRICE = Decimal("100")
RAILS = ratified_backtest_rail_policy().rails
UNCLASSIFIED = "INE011A01010"

# Seven indices, strongest first: S1 +0.7 ... S7 +0.1. The top five are S1..S5.
_INDEX_MOMENTUM = {f"S{n}": Decimal(8 - n) / 10 for n in range(1, 8)}
_TOP5 = frozenset({"S1", "S2", "S3", "S4", "S5"})
# The classified names. The two strongest classified *stocks* sit in the two weakest indices and the
# third in an unmapped industry (None): ungated they lead the basket, gated they are the names left
# out. UNCLASSIFIED is absent on purpose — it is the strongest stock of all and must pass the gate.
_NAME_INDEX: dict[str, str | None] = {
    NAMES[0]: "S7",
    NAMES[1]: "S6",
    NAMES[2]: None,
    NAMES[3]: "S1",
    NAMES[4]: "S2",
    NAMES[5]: "S3",
    NAMES[6]: "S4",
    NAMES[7]: "S5",
    NAMES[8]: "S1",
    NAMES[9]: "S3",
}


def _reading(index: str, momentum: Decimal, knowable: date) -> IndexMomentum:
    return IndexMomentum(
        index=index,
        momentum=momentum,
        start_session=knowable - timedelta(days=150),
        start_level=Decimal("100"),
        end_session=knowable,
        end_level=Decimal("100") * (1 + momentum),
        knowable_date=knowable,
    )


def _records(as_of: date) -> tuple[MomentumV2Record, ...]:
    # Stock momentum falls with position: UNCLASSIFIED (0.95) strongest, then NAMES[0] (0.9) ...
    order = (
        (UNCLASSIFIED, Decimal("0.95")),
        *((isin, Decimal(9 - n) / 10) for n, isin in enumerate(NAMES)),
    )
    return tuple(
        MomentumV2Record(
            isin=isin,
            momentum_0_12=momentum,
            momentum_12_1=momentum,
            price=PRICE,
            volatility=Decimal("0.2"),
            knowable_date=as_of,
        )
        for isin, momentum in order
    )


class _GatedData:
    """A rebalance every session, risk-on, the eleven names above, and scripted sector readings."""

    def __init__(self, momentum: dict[str, Decimal] | None = None, *, leak: bool = False) -> None:
        self._momentum = _INDEX_MOMENTUM if momentum is None else momentum
        self._leak = leak

    def is_rebalance(self, session: date) -> bool:
        return True

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        return Dataset.declaring(
            f"m@{as_of}", _records(as_of), knowable_date=lambda r: r.knowable_date
        )

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = RegimeReading(
            index_level=Decimal("110"), moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)

    def sector_index_momentum(self, as_of: date) -> Dataset[IndexMomentum]:
        knowable = as_of + timedelta(days=1) if self._leak else as_of
        readings = tuple(_reading(i, m, knowable) for i, m in self._momentum.items())
        return Dataset.declaring(f"s@{as_of}", readings, knowable_date=lambda r: r.knowable_date)

    def is_classified(self, isin: str) -> bool:
        return isin in _NAME_INDEX

    def sector_index_of(self, isin: str) -> str | None:
        return _NAME_INDEX[isin]


class _UngatedData(_GatedData):
    """The same world without the gate's reads — a source not wired for the gate."""

    sector_index_momentum = None  # type: ignore[assignment]
    is_classified = None  # type: ignore[assignment]
    sector_index_of = None  # type: ignore[assignment]


class _Broker:
    def __init__(self, holdings: tuple[Holding, ...] = ()) -> None:
        self._holdings = holdings

    def holdings(self) -> tuple[Holding, ...]:
        return self._holdings

    def positions(self) -> tuple[()]:
        return ()

    def margins(self) -> Margins:
        return Margins(available=Decimal("1000000"), utilised=Decimal("0"))


def _ctx(session: date = SESSION, broker: _Broker | None = None) -> SessionContext:
    return SessionContext(
        session=session,
        pit=PitContext(as_of=session),
        broker=broker or _Broker(),  # type: ignore[arg-type]  # the fake satisfies the reads used
        clock=FrozenClock(session),
    )


def _params(**overrides: object) -> MomentumV2Parameters:
    fields: dict[str, object] = {"top_n": 3, "regime_filter": True, **overrides}
    return MomentumV2Parameters(**fields)  # type: ignore[arg-type]


def _decide(
    params: MomentumV2Parameters, data: object, broker: _Broker | None = None
) -> SessionDecision:
    return MomentumV2Policy(data, params).decide(_ctx(broker=broker))  # type: ignore[arg-type]


def _bought(decision: SessionDecision) -> set[str]:
    return {order.isin for order in decision.orders if order.side is Side.BUY}


# ── parameters and fingerprint ───────────────────────────────────────────────────────────────────


def test_the_gate_is_off_by_default_and_d13_leaves_it_off() -> None:
    assert not MomentumV2Parameters().industry_gate
    assert not D13.industry_gate


def test_d13s_repr_is_the_pre_m16_2_rendering_so_its_run_fingerprint_is_unchanged() -> None:
    expected = (
        "MomentumV2Parameters(top_n=20, use_12_1=True, sell_band=30, regime_filter=True, "
        "vol_scaled=True, redeploy_next_session=True, vol_target_annual=None, "
        "assumed_correlation=Decimal('0.3'), regime_ma_days=200, "
        "buy_budget_fraction=Decimal('0.98'), sleeve=<Sleeve.TACTICAL: 'TACTICAL'>, "
        "parking_sleeve=<Sleeve.CASH: 'CASH'>)"
    )
    assert repr(D13) == expected
    assert repr(replace(D13, industry_gate=False)) == expected


def test_a_gated_run_spec_records_the_mapping_table_and_d13s_does_not() -> None:
    def spec(params: MomentumV2Parameters) -> dict[str, str]:
        return backtest_spec(
            "momentum_v2",
            start=date(2019, 1, 1),
            end=date(2019, 3, 31),
            parameters=params,
            opening_cash=Decimal("1000000"),
            adjusted=True,
            universe=None,
        )

    d13 = spec(D13)
    assert "sector_index_map" not in d13
    assert spec(replace(D13, industry_gate=False)) == d13
    gated = spec(D13_INDUSTRY_GATE)
    assert gated["sector_index_map"] == load_sector_index_map().sha256
    assert run_digest(gated) != run_digest(d13)


def test_the_preset_is_d13_with_only_the_gate_switched_on() -> None:
    assert replace(D13, industry_gate=True) == D13_INDUSTRY_GATE
    assert repr(D13_INDUSTRY_GATE) == repr(D13)[:-1] + ", industry_gate=True)"


# ── the ranking ──────────────────────────────────────────────────────────────────────────────────


def test_the_gate_keeps_the_five_strongest_indices_inversion() -> None:
    # Inverted (weakest-first) ranking would keep S3..S7 and admit NAMES[0]/[1]; this fails then.
    admitted, outcome = apply_industry_gate(_GatedData(), _ctx(), _records(SESSION))
    assert TOP_K == 5
    assert outcome.chosen == _TOP5
    assert [r.index for r in outcome.ranked] == [f"S{n}" for n in range(1, 8)]
    assert {r.isin for r in admitted} == {UNCLASSIFIED, *NAMES[3:]}


def test_rank_ties_break_by_index_name() -> None:
    tied = [_reading(i, Decimal("0.1"), SESSION) for i in ("SB", "SA", "SC")]
    assert [r.index for r in rank_indices(tied)] == ["SA", "SB", "SC"]


def _level(
    index: str, session: date, close: str, published: date | None = None
) -> SectorIndexLevel:
    return SectorIndexLevel(
        index=index, session=session, publication_date=published or session, close=Decimal(close)
    )


def _series(index: str, points: dict[date, str]) -> list[SectorIndexLevel]:
    return [_level(index, day, close) for day, close in points.items()]


def test_the_return_is_6_1_not_1_6_or_0_6() -> None:
    # Decision 2024-07-01: the 6m reference is 2024-01-03 (180 calendar days), the 1m 2024-06-01.
    # PEAKED rose 6m->1m then crashed in the last month; LATE was flat 6m->1m and then surged.
    # 6-1 ranks PEAKED first (+50% vs 0%). Swapping the anchors (1-6) gives PEAKED -33%, LATE 0%;
    # dropping the skip (0-6) gives PEAKED -10%, LATE +100%. Either inversion puts LATE first.
    six, one, now = date(2024, 1, 3), date(2024, 5, 31), date(2024, 7, 1)
    levels = SectorIndexLevels(
        [
            *_series("PEAKED", {six: "100", one: "150", now: "90"}),
            *_series("LATE", {six: "100", one: "100", now: "200"}),
        ]
    )
    ranked = rank_indices(levels.readings(["LATE", "PEAKED"], now))
    assert [r.index for r in ranked] == ["PEAKED", "LATE"]
    assert ranked[0].momentum == Decimal("0.5")
    assert (ranked[0].start_session, ranked[0].end_session) == (six, one)


def test_with_the_gate_the_basket_comes_from_the_top_indices_only() -> None:
    gated = _decide(_params(industry_gate=True), _GatedData())
    ungated = _decide(_params(), _GatedData())
    assert _bought(ungated) == {UNCLASSIFIED, NAMES[0], NAMES[1]}
    assert _bought(gated) == {UNCLASSIFIED, NAMES[3], NAMES[4]}


# ── classified, unmapped, unclassified ───────────────────────────────────────────────────────────


def test_unclassified_names_are_gate_neutral_inversion() -> None:
    # If "unclassified" were read as "ineligible" (the survivorship bias B1 removed), the strongest
    # stock would be dropped and this fails.
    admitted, outcome = apply_industry_gate(_GatedData(), _ctx(), _records(SESSION))
    assert UNCLASSIFIED in {r.isin for r in admitted}
    assert outcome.unclassified_admitted == 1
    assert UNCLASSIFIED in _bought(_decide(_params(industry_gate=True), _GatedData()))


def test_a_classified_name_in_an_unmapped_industry_is_excluded_inversion() -> None:
    # NAMES[2] is classified, industry unmapped. Every mapped index is in the top five here, so the
    # only thing that can drop it is "unmapped means ineligible"; were unmapped treated like
    # unclassified (neutral), it would be bought.
    momentum = {"S1": Decimal("0.3"), "S6": Decimal("0.2"), "S7": Decimal("0.1")}
    decision = _decide(_params(industry_gate=True, top_n=4), _GatedData(momentum))
    assert NAMES[2] not in _bought(decision)
    assert _bought(decision) == {UNCLASSIFIED, NAMES[0], NAMES[1], NAMES[3]}


def test_a_classified_name_in_a_non_top_5_index_is_excluded() -> None:
    admitted, _ = apply_industry_gate(_GatedData(), _ctx(), _records(SESSION))
    assert not {NAMES[0], NAMES[1]} & {r.isin for r in admitted}  # S7, S6: ranks 7 and 6


def test_the_map_tells_unclassified_from_unmapped() -> None:
    index_map = load_sector_index_map()
    unmapped = next(
        isin for isin, industry in index_map.sectors.by_isin.items() if industry == "Capital Goods"
    )
    assert index_map.is_classified(unmapped)
    assert index_map.index_of(unmapped) is None
    assert not index_map.is_classified("INE000X00000")
    with pytest.raises(SectorIndexError, match="not in the classification"):
        index_map.index_of("INE000X00000")


def test_a_held_name_whose_index_drops_out_is_sold() -> None:
    held = (Holding(isin=NAMES[0], exchange=Exchange.NSE, quantity=10, average_price=PRICE),)
    gated = _decide(_params(industry_gate=True, sell_band=11), _GatedData(), _Broker(held))
    ungated = _decide(_params(sell_band=11), _GatedData(), _Broker(held))
    assert {o.isin for o in gated.orders if o.side is Side.SELL} == {NAMES[0]}
    assert not [o for o in ungated.orders if o.side is Side.SELL]


def test_fewer_than_five_rankable_indices_keeps_all_and_none_leaves_only_the_unclassified() -> None:
    two = _decide(
        _params(industry_gate=True),
        _GatedData({"S6": Decimal("-0.2"), "S7": Decimal("-0.3")}),
    )
    assert _bought(two) == {UNCLASSIFIED, NAMES[0], NAMES[1]}
    none = _decide(_params(industry_gate=True), _GatedData({}))
    assert _bought(none) == {UNCLASSIFIED}
    assert any("only unclassified" in (item.text or "") for item in none.evidence.items)


def test_the_decision_evidence_records_the_ranking_and_the_neutral_count() -> None:
    decision = _decide(_params(industry_gate=True), _GatedData())
    items = [i for i in decision.evidence.items if i.label == "sector_index_momentum_6_1"]
    assert [(i.detail["index"], i.detail["kept"]) for i in items] == [
        (f"S{n}", "true" if n <= 5 else "false") for n in range(1, 8)
    ]
    (neutral,) = [
        i for i in decision.evidence.items if i.label == "industry_gate_unclassified_admitted"
    ]
    assert neutral.value == Decimal(1)


def test_a_source_not_wired_for_the_gate_is_refused() -> None:
    with pytest.raises(TypeError, match="IndustryGateData"):
        _decide(_params(industry_gate=True), _UngatedData())


def test_an_outcome_with_no_ranking_still_annotates() -> None:
    outcome = IndustryGateOutcome(ranked=(), chosen=frozenset())
    assert len(outcome.evidence_items(SESSION)) == 2


# ── point in time ────────────────────────────────────────────────────────────────────────────────


def test_a_future_dated_sector_reading_trips_the_pit_guard() -> None:
    with pytest.raises(PitError):
        _decide(_params(industry_gate=True), _GatedData(leak=True))


def test_with_the_gate_off_the_sector_readings_are_never_read() -> None:
    # A leaking sector source is harmless to an ungated policy: it never asks.
    assert _bought(_decide(_params(), _GatedData(leak=True))) == {UNCLASSIFIED, NAMES[0], NAMES[1]}


def test_a_level_published_after_the_decision_date_is_refused() -> None:
    levels = SectorIndexLevels(
        [
            _level("X", date(2024, 1, 1), "100"),
            # The level *for* 2 January was published only on 1 March.
            _level("X", date(2024, 1, 2), "200", published=date(2024, 3, 1)),
        ]
    )
    before = levels.level_on_or_before("X", date(2024, 1, 2), as_of=date(2024, 2, 1))
    after = levels.level_on_or_before("X", date(2024, 1, 2), as_of=date(2024, 3, 1))
    assert before is not None and before.close == Decimal("100")
    assert after is not None and after.close == Decimal("200")


def test_a_reading_is_dated_by_its_later_publication_so_the_guard_rechecks_it() -> None:
    as_of = date(2024, 7, 1)
    levels = SectorIndexLevels(
        [_level("X", date(2024, 1, 2), "100"), _level("X", date(2024, 5, 31), "120")]
    )
    reading = levels.momentum("X", as_of)
    assert reading is not None
    assert reading.momentum == Decimal("0.2")
    assert reading.knowable_date == date(2024, 5, 31)
    assert (reading.start_session, reading.end_session) == (date(2024, 1, 2), date(2024, 5, 31))


def _daily(
    index: str, first: date, last: date, start: Decimal = Decimal("100")
) -> list[SectorIndexLevel]:
    days = (first + timedelta(days=n) for n in range((last - first).days + 1))
    return [
        _level(index, day, str(start + n))
        for n, day in enumerate(d for d in days if d.weekday() < 5)
    ]


def test_an_index_that_starts_mid_window_is_rankable_only_180_days_after_its_first_level() -> None:
    first = date(2024, 3, 1)
    levels = SectorIndexLevels(_daily("NEW", first, date(2025, 3, 31)))
    # Six-month reference on 2024-07-01 is 2024-01-03 — before the series exists: no reading, and
    # no backfill from the first published level.
    assert levels.momentum("NEW", date(2024, 7, 1)) is None
    assert levels.momentum("NEW", date(2024, 8, 27)) is None  # ref 2024-02-29: still before
    reading = levels.momentum("NEW", date(2024, 8, 28))  # ref 2024-03-01: the first level
    assert reading is not None
    assert reading.start_session == first


def test_a_mid_window_index_is_not_ranked_before_it_is_rankable() -> None:
    old = _daily("OLD", date(2023, 1, 2), date(2025, 3, 31))
    # NEW rockets from its first level; it must still be invisible to the July 2024 gate.
    new = [
        _level("NEW", lv.session, str(lv.close * 10))
        for lv in _daily("NEW", date(2024, 3, 1), date(2025, 3, 31))
    ]
    levels = SectorIndexLevels([*old, *new])
    assert [r.index for r in levels.readings(["NEW", "OLD"], date(2024, 7, 1))] == ["OLD"]
    assert [r.index for r in levels.readings(["NEW", "OLD"], date(2024, 9, 2))] == ["NEW", "OLD"]


def test_a_series_that_stopped_publishing_is_not_carried_forward() -> None:
    levels = SectorIndexLevels(_daily("DEAD", date(2015, 1, 1), date(2015, 11, 6)))
    last = date(2015, 11, 6)
    assert levels.level_on_or_before(
        "DEAD", last + timedelta(days=MAX_STALE_DAYS), as_of=date(2016, 1, 1)
    )
    assert (
        levels.level_on_or_before(
            "DEAD", last + timedelta(days=MAX_STALE_DAYS + 1), as_of=date(2016, 1, 1)
        )
        is None
    )
    assert levels.momentum("DEAD", date(2016, 7, 1)) is None


def test_an_identical_repeated_level_is_one_fact() -> None:
    # The 2022-03-07 PR bundle lists every index row twice, byte for byte.
    levels = SectorIndexLevels(
        [_level("X", date(2024, 1, 1), "1"), _level("X", date(2024, 1, 1), "1")]
    )
    assert levels.first_session("X") == date(2024, 1, 1)


def test_two_levels_for_one_index_and_session_are_refused() -> None:
    with pytest.raises(SectorIndexError, match="two levels"):
        SectorIndexLevels([_level("X", date(2024, 1, 1), "1"), _level("X", date(2024, 1, 1), "2")])


def _write_partition(
    root: Path, session: date, rows: list[tuple[str, str]], published: date | None = None
) -> None:
    directory = root / "L1" / "pr_index_eod" / f"date={session.isoformat()}"
    directory.mkdir(parents=True)
    table = pa.table(
        {
            "session": pa.array([session] * len(rows), pa.date32()),
            "publication_date": pa.array([published or session] * len(rows), pa.date32()),
            "index_id": pa.array([index_id for index_id, _ in rows]),
            "close": pa.array([Decimal(close) for _, close in rows], pa.decimal128(20, 4)),
        }
    )
    pq.write_table(table, directory / "part.parquet")


def test_the_l1_loader_chains_renamed_ids_and_skips_unmapped_ones(tmp_path: Path) -> None:
    index_map = load_sector_index_map()
    _write_partition(tmp_path, date(2015, 11, 6), [("CNX AUTO", "8144.45"), ("NIFTY BANK", "1")])
    _write_partition(tmp_path, date(2015, 11, 9), [("Nifty Auto", "8246.2")])
    _write_partition(tmp_path, date(2015, 11, 10), [("Nifty Auto", "8300")])
    levels = SectorIndexLevels.from_l1(index_map, through=date(2015, 11, 9), data_root=tmp_path)
    assert levels.first_session("NIFTY AUTO") == date(2015, 11, 6)
    assert levels.first_session("NIFTY BANK") is None  # not a mapped index
    got = levels.level_on_or_before("NIFTY AUTO", date(2015, 11, 12), as_of=date(2015, 12, 1))
    assert got is not None and got.close == Decimal("8246.2")  # 10 Nov is past `through`


def test_the_l1_loader_never_reads_a_row_published_after_the_decision(tmp_path: Path) -> None:
    index_map = load_sector_index_map()
    _write_partition(tmp_path, date(2024, 1, 1), [("NIFTY IT", "100")])
    _write_partition(tmp_path, date(2024, 1, 2), [("NIFTY IT", "150")], published=date(2024, 6, 1))
    levels = SectorIndexLevels.from_l1(index_map, through=date(2024, 1, 2), data_root=tmp_path)
    got = levels.level_on_or_before("NIFTY IT", date(2024, 1, 2), as_of=date(2024, 2, 1))
    assert got is not None and got.close == Decimal("100")


# ── report helpers ───────────────────────────────────────────────────────────────────────────────


def test_unclassified_shares_per_period() -> None:
    index_map = load_sector_index_map()
    classified = sorted(index_map.sectors.by_isin)[:3]
    shares = unclassified_shares(
        index_map,
        {
            "2017": [*classified, "INE000X00001"],
            "2016": ["INE000X00001", "INE000X00002", classified[0], classified[0]],
            "empty": [],
        },
    )
    assert shares == (
        UnclassifiedShare("2016", 3, 2, Decimal(2) / Decimal(3)),
        UnclassifiedShare("2017", 4, 1, Decimal("0.25")),
        UnclassifiedShare("empty", 0, 0, Decimal(0)),
    )


def test_first_rankable_dates_are_180_calendar_days_after_the_first_level() -> None:
    index_map = load_sector_index_map()
    levels = SectorIndexLevels(
        [
            *_daily("NIFTY IT", date(2024, 3, 1), date(2025, 3, 31)),
            # Starts on a Saturday, so its first level is Monday 4 March.
            *_daily("NIFTY AUTO", date(2024, 3, 2), date(2025, 3, 31)),
        ]
    )
    found = first_rankable_dates(levels, index_map)
    assert set(found) == set(index_map.indices)
    assert found["NIFTY IT"] == date(2024, 8, 28)
    assert found["NIFTY AUTO"] == date(2024, 3, 4) + timedelta(days=180)
    assert found["NIFTY METAL"] is None
    for index, day in found.items():  # the date found is genuinely the first rankable one
        if day is not None:
            assert levels.momentum(index, day) is not None
            assert levels.momentum(index, day - timedelta(days=1)) is None


# ── the mapping table ────────────────────────────────────────────────────────────────────────────

_UNMAPPED = (
    "Capital Goods",
    "Consumer Services",
    "Diversified",
    "Forest Materials",
    "Services",
    "Telecommunication",
    "Textiles",
    "Utilities",
)


def test_the_reviewed_table_covers_the_ratified_classification_exactly() -> None:
    index_map = load_sector_index_map()
    sectors = ratified_sector_map()
    assert set(index_map.industry_index) == set(sectors.by_isin.values())
    assert index_map.unmapped_industries == _UNMAPPED
    assert len(index_map.indices) == 12
    assert "NIFTY BANK" not in index_map.indices  # narrower than the industry; not usable
    mapped = sum(1 for isin in sectors.by_isin if index_map.index_of(isin) is not None)
    # 536 of the classification's 755 names; a change here is a mapping change to review.
    assert (mapped, len(sectors.by_isin)) == (536, 755)


def _table_with(tmp_path: Path, edit: Callable[[dict[str, Any]], object]) -> Path:
    doc = yaml.safe_load(SECTOR_INDEX_MAP_PATH.read_text())
    edit(doc)
    path = tmp_path / "map.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda d: d["industries"].pop("Textiles"), "no mapping decision"),
        (
            lambda d: d["industries"].update({"Shipbuilding": {"index": None, "why": "x"}}),
            "not in the classification",
        ),
        (lambda d: d["industries"]["Realty"].update({"index": "NIFTY BANK"}), "undeclared index"),
        (lambda d: d["industries"]["Realty"].update({"why": " "}), "no stated reason"),
        (lambda d: d["indices"]["NIFTY IT"].update({"aliases": ["CNX AUTO"]}), "aliased to"),
        (
            lambda d: d["indices"].update({"NIFTY BANK": {"aliases": ["NIFTY BANK"]}}),
            "mapped from no",
        ),
        (lambda d: d["classification"].update({"sha256": "0" * 64}), "re-review"),
    ],
)
def test_an_inconsistent_table_is_refused(
    tmp_path: Path, edit: Callable[[dict[str, Any]], object], match: str
) -> None:
    with pytest.raises(SectorIndexError, match=match):
        load_sector_index_map(_table_with(tmp_path, edit))


# ── the real stack: D13 unchanged, the gate wired end to end ─────────────────────────────────────
# A local copy of the M14.5 scripted world (tests/unit/test_momentum_v2_daily_regime.py): Q1 2024
# weekdays, ten names at a flat price, a momentum order that rotates monthly, a scripted regime.

_SESSIONS = tuple(
    day for day in (date(2024, 1, 1) + timedelta(days=n) for n in range(91)) if day.weekday() < 5
)
_REBALANCES = frozenset(
    min(s for s in _SESSIONS if s.month == month) for month in {s.month for s in _SESSIONS}
)
_OPENING = Decimal("1000000")

#: D13's digest over :func:`_replay`, struck with the policy before M16.2 (main 523882e; the same
#: value M14.5 pins). Journal, book and rails byte-for-byte: if the gate-off path drifts in any
#: order, entry or fill, this moves.
_D13_DIGEST_BEFORE_M16_2 = "67d887030dd04994c4f675992924b893a108030d0b3d6270dd40963daf879b28"


class _Market:
    def __init__(self, sessions: tuple[date, ...]) -> None:
        self._calendar = (*sessions, date(2024, 12, 31))

    def next_session(self, after: date) -> date:
        return next(session for session in self._calendar if session > after)

    def reference_bar(self, isin: str, session: date) -> ReferenceBar:
        if isin not in NAMES:
            raise NoReferenceBarError(isin)
        return ReferenceBar(
            isin=isin,
            session=session,
            exchange=Exchange.NSE,
            open=PRICE,
            vwap=PRICE,
            traded_value=Decimal("100000000000"),
        )


def _risk_on(session: date) -> bool:
    return date(2024, 1, 10) <= session < date(2024, 2, 15) or session >= date(2024, 2, 29)


class _ScriptedData:
    """Ten names, a momentum order that rotates each month, and the scripted regime above."""

    def is_rebalance(self, session: date) -> bool:
        return session in _REBALANCES

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        shift = as_of.month
        records = tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=Decimal((index + shift) % 10) / 10,
                momentum_12_1=Decimal((index * 3 + shift) % 10) / 10,
                price=PRICE,
                volatility=Decimal("0.1") + Decimal(index) / 100,
                knowable_date=as_of,
            )
            for index, isin in enumerate(NAMES)
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        level = Decimal("105") if _risk_on(as_of) else Decimal("95")
        reading = RegimeReading(
            index_level=level, moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


class _ScriptedGatedData(_ScriptedData):
    """Plus sectors: names 0-4 in one index, 5-8 in an unmapped industry, 9 unclassified."""

    def sector_index_momentum(self, as_of: date) -> Dataset[IndexMomentum]:
        readings = (_reading("UP", Decimal("0.2"), as_of),)
        return Dataset.declaring(f"s@{as_of}", readings, knowable_date=lambda r: r.knowable_date)

    def is_classified(self, isin: str) -> bool:
        return isin != NAMES[9]

    def sector_index_of(self, isin: str) -> str | None:
        return "UP" if NAMES.index(isin) < 5 else None


def _replay(params: MomentumV2Parameters, data: _ScriptedData | None = None) -> ReplayResult:
    clock = FrozenClock(_SESSIONS[0])
    sim = SimBroker(
        clock=clock,
        cost_model=CostModel(load_rate_card(), account_state="MH"),
        market=_Market(_SESSIONS),
        opening_cash=_OPENING,
    )
    book = PortfolioBook()
    book.deposit(_SESSIONS[0], _OPENING)
    broker = _AccountingBroker(sim, book, corporate_actions=BookActionCalendar())
    rail_policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RAILS,
        sectors=SectorMap(source="test", sha256="test", by_isin={i: i[3:6] for i in NAMES}),
        provenance="test",
    )
    prices = {(isin, day): PRICE for isin in NAMES for day in _SESSIONS}
    return ReplayEngine(
        policy=MomentumV2Policy(data or _ScriptedData(), params, order_caps=RAILS),
        broker=broker,
        clock=clock,
        sessions=_SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()


def test_gate_off_reproduces_d13s_pre_m16_2_replay_byte_for_byte() -> None:
    assert _replay(D13).digest() == _D13_DIGEST_BEFORE_M16_2
    assert _replay(replace(D13, industry_gate=False)).digest() == _D13_DIGEST_BEFORE_M16_2


def test_the_gated_preset_replays_and_buys_only_top_index_or_unclassified_names() -> None:
    gated = _replay(D13_INDUSTRY_GATE, _ScriptedGatedData())
    ungated = _replay(D13, _ScriptedGatedData())
    assert ungated.digest() == _D13_DIGEST_BEFORE_M16_2  # the gate's reads change nothing off
    assert gated.digest() != ungated.digest()
    bought = {e.isin for e in gated.journal if e.decision is Decision.BUY}
    assert bought and bought <= {*NAMES[:5], NAMES[9]}
    assert {e.isin for e in ungated.journal if e.decision is Decision.BUY} & set(NAMES[5:9])
