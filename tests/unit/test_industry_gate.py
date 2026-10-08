"""M16.2 — momentum v2's point-in-time industry-momentum gate (off by default).

The gate ranks NSE sectoral indices by their 6-1 month return from *published* levels, keeps the top
five, and admits only names whose industry maps to one of them. The tests here pin, each with an
inverted twin where the rule has a direction: the ranking keeps the strongest indices (not the
weakest); a level published after the decision date is never read, and a reading dated after it
trips the PIT guard; an index that starts mid-window is rankable only six months after its first
published level; unmapped and unclassified names are excluded; the reviewed mapping table covers the
classification exactly; and with the gate off D13's replay digest and run fingerprint are the ones
struck before M16.2.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

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
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap, ratified_sector_map
from backtest.replay import ReplayEngine, ReplayResult, SessionContext, SessionDecision
from backtest.run import _AccountingBroker
from backtest.sector_indices import (
    MAX_STALE_DAYS,
    SECTOR_INDEX_MAP_PATH,
    SectorIndexError,
    SectorIndexLevel,
    SectorIndexLevels,
    load_sector_index_map,
)
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset, PitContext, PitError
from execution.broker import Exchange, Holding, Margins, Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker
from tests.rails_support import marks_from
from tests.unit.test_buy_sizing_ceiling import NAMES, PRICE, RAILS, _Market
from tests.unit.test_momentum_v2_daily_regime import (
    _D13_DIGEST_BEFORE_M14_5,
    _OPENING,
    _SESSIONS,
    _ScriptedData,
)
from tests.unit.test_momentum_v2_daily_regime import _replay as _d13_replay

D13 = PAPER_RATIFIED_2026_09_06
SESSION = date(2024, 7, 1)

# Seven indices, strongest first: S1 +0.7 ... S7 +0.1. The top five are S1..S5.
_INDEX_MOMENTUM = {f"S{n}": Decimal(8 - n) / 10 for n in range(1, 8)}
_TOP5 = frozenset({"S1", "S2", "S3", "S4", "S5"})
# Ten names. The two strongest *stocks* sit in the two weakest indices, and the third in no index:
# without the gate they lead the basket; with it they must be the names left out.
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
    # Stock momentum falls with the name's position: NAMES[0] strongest.
    return tuple(
        MomentumV2Record(
            isin=isin,
            momentum_0_12=Decimal(10 - n) / 10,
            momentum_12_1=Decimal(10 - n) / 10,
            price=PRICE,
            volatility=Decimal("0.2"),
            knowable_date=as_of,
        )
        for n, isin in enumerate(NAMES)
    )


class _GatedData:
    """A rebalance every session, risk-on, the ten names above, and scripted sector readings."""

    def __init__(
        self,
        momentum: dict[str, Decimal] | None = None,
        *,
        leak: bool = False,
        name_index: dict[str, str | None] | None = None,
    ) -> None:
        self._momentum = _INDEX_MOMENTUM if momentum is None else momentum
        self._leak = leak
        self._name_index = _NAME_INDEX if name_index is None else name_index

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

    def sector_index_of(self, isin: str) -> str | None:
        return self._name_index.get(isin)


class _UngatedData(_GatedData):
    """The same world without the gate's reads — a source not wired for the gate."""

    sector_index_momentum = None  # type: ignore[assignment]
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
    assert {r.isin for r in admitted} == set(NAMES[3:])


def test_rank_ties_break_by_index_name() -> None:
    tied = [_reading(i, Decimal("0.1"), SESSION) for i in ("SB", "SA", "SC")]
    assert [r.index for r in rank_indices(tied)] == ["SA", "SB", "SC"]


def test_with_the_gate_the_basket_comes_from_the_top_indices_only() -> None:
    gated = _decide(_params(industry_gate=True), _GatedData())
    ungated = _decide(_params(), _GatedData())
    assert _bought(ungated) == {NAMES[0], NAMES[1], NAMES[2]}
    assert _bought(gated) == {NAMES[3], NAMES[4], NAMES[5]}


def test_unmapped_names_are_excluded_even_when_their_stock_momentum_leads() -> None:
    # NAMES[2] (no index) is the third-strongest stock; every index is in the top five here.
    momentum = {"S1": Decimal("0.3"), "S6": Decimal("0.2"), "S7": Decimal("0.1")}
    decision = _decide(_params(industry_gate=True), _GatedData(momentum))
    assert NAMES[2] not in _bought(decision)
    assert {NAMES[0], NAMES[1]} <= _bought(decision)


def test_a_held_name_whose_index_drops_out_is_sold() -> None:
    held = (Holding(isin=NAMES[0], exchange=Exchange.NSE, quantity=10, average_price=PRICE),)
    gated = _decide(_params(industry_gate=True, sell_band=10), _GatedData(), _Broker(held))
    ungated = _decide(_params(sell_band=10), _GatedData(), _Broker(held))
    assert {o.isin for o in gated.orders if o.side is Side.SELL} == {NAMES[0]}
    assert not [o for o in ungated.orders if o.side is Side.SELL]


def test_fewer_than_five_rankable_indices_keeps_all_of_them_and_none_admits_nobody() -> None:
    two = _decide(
        _params(industry_gate=True), _GatedData({"S6": Decimal("-0.2"), "S7": Decimal("-0.3")})
    )
    assert _bought(two) == {NAMES[0], NAMES[1]}
    none = _decide(_params(industry_gate=True), _GatedData({}))
    assert not none.orders
    assert any("none eligible" in (item.text or "") for item in none.evidence.items)


def test_the_decision_evidence_records_the_ranking() -> None:
    decision = _decide(_params(industry_gate=True), _GatedData())
    items = [i for i in decision.evidence.items if i.label == "sector_index_momentum_6_1"]
    assert [(i.detail["index"], i.detail["kept"]) for i in items] == [
        (f"S{n}", "true" if n <= 5 else "false") for n in range(1, 8)
    ]


def test_a_source_not_wired_for_the_gate_is_refused() -> None:
    with pytest.raises(TypeError, match="IndustryGateData"):
        _decide(_params(industry_gate=True), _UngatedData())


def test_an_outcome_with_no_ranking_still_annotates() -> None:
    outcome = IndustryGateOutcome(ranked=(), chosen=frozenset())
    assert len(outcome.evidence_items(SESSION)) == 1


# ── point in time ────────────────────────────────────────────────────────────────────────────────


def test_a_future_dated_sector_reading_trips_the_pit_guard() -> None:
    with pytest.raises(PitError):
        _decide(_params(industry_gate=True), _GatedData(leak=True))


def test_with_the_gate_off_the_sector_readings_are_never_read() -> None:
    # A leaking sector source is harmless to an ungated policy: it never asks.
    assert _bought(_decide(_params(), _GatedData(leak=True))) == {NAMES[0], NAMES[1], NAMES[2]}


def _level(
    index: str, session: date, close: str, published: date | None = None
) -> SectorIndexLevel:
    return SectorIndexLevel(
        index=index, session=session, publication_date=published or session, close=Decimal(close)
    )


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


def test_an_index_that_starts_mid_window_is_rankable_only_six_months_after_its_first_level() -> (
    None
):
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
    assert levels.level_on_or_before(
        "DEAD", date(2015, 11, 6) + timedelta(days=MAX_STALE_DAYS), as_of=date(2016, 1, 1)
    )
    assert (
        levels.level_on_or_before(
            "DEAD", date(2015, 11, 6) + timedelta(days=MAX_STALE_DAYS + 1), as_of=date(2016, 1, 1)
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


def test_an_unclassified_isin_maps_to_nothing() -> None:
    assert load_sector_index_map().index_of("INE000X00000") is None


def _table_with(tmp_path: Path, edit: object) -> Path:
    doc = yaml.safe_load(SECTOR_INDEX_MAP_PATH.read_text())
    edit(doc)  # type: ignore[operator]
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
def test_an_inconsistent_table_is_refused(tmp_path: Path, edit: object, match: str) -> None:
    with pytest.raises(SectorIndexError, match=match):
        load_sector_index_map(_table_with(tmp_path, edit))


# ── the real stack: D13 unchanged, the gate wired end to end ─────────────────────────────────────


def test_gate_off_reproduces_d13s_pre_m16_2_replay_byte_for_byte() -> None:
    # The digest was struck with the policy before M16.2 (and before M14.5) — journal, book, rails.
    assert _d13_replay(D13).digest() == _D13_DIGEST_BEFORE_M14_5
    assert _d13_replay(replace(D13, industry_gate=False)).digest() == _D13_DIGEST_BEFORE_M14_5


class _ScriptedGatedData(_ScriptedData):
    """The M14.5 scripted world plus sectors: names 0-4 in a rising one, 5-9 in none."""

    def sector_index_momentum(self, as_of: date) -> Dataset[IndexMomentum]:
        readings = (_reading("UP", Decimal("0.2"), as_of), _reading("DOWN", Decimal("-0.2"), as_of))
        return Dataset.declaring(f"s@{as_of}", readings, knowable_date=lambda r: r.knowable_date)

    def sector_index_of(self, isin: str) -> str | None:
        return "UP" if NAMES.index(isin) < 5 else None


def _gated_replay(params: MomentumV2Parameters) -> ReplayResult:
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
        policy=MomentumV2Policy(_ScriptedGatedData(), params, order_caps=RAILS),
        broker=broker,
        clock=clock,
        sessions=_SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()


def test_the_gated_preset_replays_and_buys_only_mapped_top_index_names() -> None:
    gated = _gated_replay(D13_INDUSTRY_GATE)
    ungated = _gated_replay(D13)
    assert ungated.digest() == _D13_DIGEST_BEFORE_M14_5  # the gate's reads change nothing while off
    assert gated.digest() != ungated.digest()
    bought = {e.isin for e in gated.journal if e.decision is Decision.BUY}
    assert bought and bought <= set(NAMES[:5])
    assert {e.isin for e in ungated.journal if e.decision is Decision.BUY} - set(NAMES[:5])
