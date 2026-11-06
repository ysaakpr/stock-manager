"""M14.9 arm S — rank-ordered buys under A8's sector cap, and the sector refill (off by default).

A8's 35 % sector rail clears a session's buys one at a time, in the order the policy submits them,
so which names of a crowded sector it refuses depends on that order. ``rank_ordered_buys`` submits
buys best-ranked first, so the cap refuses the lowest-ranked; ``sector_refill`` replaces a name
the cap would refuse with the next-ranked name outside its sector. The rails are never touched.

Each behaviour has an inverted twin; a property test drives generated rankings and sector maps
through the real rail gate and checks no sector ever ends above the cap; and D13 with both
switches off is byte-for-byte the pre-M14.5 replay.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from analyst.journal.models import Decision
from backtest.accounting import PortfolioBook
from backtest.book_actions import BookActionCalendar
from backtest.policies.momentum_v2 import (
    D13_RANK_ORDERED_BUYS,
    D13_SECTOR_REFILL,
    PAPER_RATIFIED_2026_09_06,
    MomentumV2Parameters,
    MomentumV2Policy,
    MomentumV2Record,
    RegimeReading,
)
from backtest.rails import BacktestRailPolicy, RailGate, SectorMap
from backtest.replay import ReplayEngine, ReplayResult
from backtest.run import _AccountingBroker
from dataplatform.clock import FrozenClock
from dataplatform.query.pit import Dataset
from execution.broker import Side
from execution.costs import CostModel, load_rate_card
from execution.sim_broker import SimBroker
from tests.rails_support import marks_from
from tests.unit.test_buy_sizing_ceiling import NAMES, PRICE, RAILS, _Market
from tests.unit.test_momentum_v2_daily_regime import _D13_DIGEST_BEFORE_M14_5, _Broker, _ctx
from tests.unit.test_momentum_v2_daily_regime import _held as _held_names
from tests.unit.test_momentum_v2_daily_regime import _replay as _m14_5_replay

D13 = PAPER_RATIFIED_2026_09_06
_SESSIONS = tuple(
    d for d in (date(2024, 1, 1) + timedelta(days=n) for n in range(10)) if d.weekday() < 5
)
_OPENING = Decimal("1000000")
CAP = RAILS.max_sector_pct / Decimal(100)


class _Data:
    """One rebalance (the first session), always risk-on; ``order`` is the momentum ranking."""

    def __init__(self, order: tuple[str, ...]) -> None:
        self._momentum = {isin: Decimal(len(order) - i) / 10 for i, isin in enumerate(order)}

    def is_rebalance(self, session: date) -> bool:
        return session == _SESSIONS[0]

    def signal(self, as_of: date) -> Dataset[MomentumV2Record]:
        records = tuple(
            MomentumV2Record(
                isin=isin,
                momentum_0_12=m,
                momentum_12_1=m,
                price=PRICE,
                volatility=Decimal("0.2"),
                knowable_date=as_of,
            )
            for isin, m in self._momentum.items()
        )
        return Dataset.declaring(f"m@{as_of}", records, knowable_date=lambda r: r.knowable_date)

    def regime(self, as_of: date) -> Dataset[RegimeReading]:
        reading = RegimeReading(
            index_level=Decimal("105"), moving_average=Decimal("100"), knowable_date=as_of
        )
        return Dataset.declaring(f"r@{as_of}", (reading,), knowable_date=lambda r: r.knowable_date)


def _replay(
    params: MomentumV2Parameters, order: tuple[str, ...], sectors: dict[str, str]
) -> tuple[ReplayResult, PortfolioBook]:
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
    sector_map = SectorMap(source="test", sha256="test", by_isin=sectors)
    rail_policy = BacktestRailPolicy(
        policy_id="test-ratified-caps",
        version=1,
        rails=RAILS,
        sectors=sector_map,
        provenance="test",
    )
    prices = {(isin, day): PRICE for isin in NAMES for day in _SESSIONS}
    result = ReplayEngine(
        policy=MomentumV2Policy(
            _Data(order), params, order_caps=RAILS, sector_of=sector_map.sector_of
        ),
        broker=broker,
        clock=clock,
        sessions=_SESSIONS,
        rails=RailGate(rail_policy, marks_from(prices)),
    ).run()
    return result, book


def _held(book: PortfolioBook) -> set[str]:
    return {p.isin for p in book.positions() if p.quantity > 0}


def _sector_blocks(result: ReplayResult) -> int:
    return result.rail_blocks.get("MAX_SECTOR", 0)


# Sector X holds six names; the momentum ranking runs *against* ISIN order inside it, so a gate
# fed in ISIN order refuses the best-ranked X names and one fed in rank order the worst-ranked.
_X = NAMES[:6]
_SECTORS = {isin: ("X" if isin in _X else f"S{i}") for i, isin in enumerate(NAMES)}
_RANKING = (*reversed(_X), *NAMES[6:])  # NAMES[5] is the best name of all, NAMES[0] the worst X


# ── parameters ───────────────────────────────────────────────────────────────────────────────────


def test_d13s_fingerprint_and_replay_are_unchanged_with_both_switches_off() -> None:
    assert not D13.rank_ordered_buys and not D13.sector_refill
    assert repr(replace(D13, rank_ordered_buys=False, sector_refill=False)) == repr(D13)
    assert _m14_5_replay(D13).digest() == _D13_DIGEST_BEFORE_M14_5
    off = replace(D13, rank_ordered_buys=False, sector_refill=False)
    assert _m14_5_replay(off).digest() == _D13_DIGEST_BEFORE_M14_5


def test_each_arm_has_its_own_fingerprint() -> None:
    assert len({repr(D13), repr(D13_RANK_ORDERED_BUYS), repr(D13_SECTOR_REFILL)}) == 3
    assert replace(D13_SECTOR_REFILL, rank_ordered_buys=False, sector_refill=False) == D13


def test_the_switches_are_validated() -> None:
    with pytest.raises(ValueError, match="rank_ordered_buys"):
        replace(D13, sector_refill=True)
    with pytest.raises(ValueError, match="vol_target_annual"):
        replace(D13_RANK_ORDERED_BUYS, vol_target_annual=Decimal("0.15"))
    with pytest.raises(TypeError, match="sector map"):
        MomentumV2Policy(_Data(NAMES), D13_SECTOR_REFILL, order_caps=RAILS)


# ── rank order at the gate ───────────────────────────────────────────────────────────────────────


def test_rank_ordered_buys_let_the_cap_refuse_the_lowest_ranked_names() -> None:
    _, book = _replay(D13_RANK_ORDERED_BUYS, _RANKING, _SECTORS)
    held_x = _held(book) & set(_X)
    best_x = set(_RANKING[: len(held_x)])
    assert held_x and held_x < set(_X)  # the cap bound
    assert held_x == best_x  # and it kept the best-ranked X names


def test_without_rank_order_the_gate_keeps_whichever_names_came_first_inversion() -> None:
    # D13 submits buys in its allocator's order, not by rank: here it keeps the worst X names.
    _, book = _replay(D13, _RANKING, _SECTORS)
    _, ranked_book = _replay(D13_RANK_ORDERED_BUYS, _RANKING, _SECTORS)
    held_x, ranked_x = _held(book) & set(_X), _held(ranked_book) & set(_X)
    assert len(held_x) == len(ranked_x)
    assert held_x != ranked_x


def test_a_rank_order_inverted_keeps_the_worst_names_inversion() -> None:
    # Reverse the ranking and the same switch must keep the other end of sector X.
    inverted = (*_X, *NAMES[6:])
    _, book = _replay(D13_RANK_ORDERED_BUYS, inverted, _SECTORS)
    held_x = _held(book) & set(_X)
    assert held_x == set(inverted[: len(held_x)])
    assert NAMES[5] not in held_x


# ── the refill ───────────────────────────────────────────────────────────────────────────────────

_SMALL = replace(D13_RANK_ORDERED_BUYS, top_n=6, sell_band=6)
_SMALL_REFILL = replace(_SMALL, sector_refill=True)


def test_the_refill_replaces_capped_names_with_the_next_ranked_outside_the_sector() -> None:
    # Ranks 1-4 are X, 5-10 elsewhere. Six slots at ~1/6 each: X takes two (33%), a third would
    # breach 35%. S leaves those slots in cash; the refill buys ranks 5-8 instead.
    ranking = (*_X[:4], *NAMES[6:])
    capped, capped_book = _replay(_SMALL, ranking, _SECTORS)
    refilled, refilled_book = _replay(_SMALL_REFILL, ranking, _SECTORS)
    assert _sector_blocks(capped) > 0
    assert _sector_blocks(refilled) == 0
    assert _held(refilled_book) == {*_X[:2], *NAMES[6:10]}
    assert len(_held(refilled_book)) > len(_held(capped_book))
    # Inversion: the refill keeps the *best* X names, never the ones below them.
    assert not _held(refilled_book) & set(_X[2:])


def test_the_refill_changes_nothing_when_the_cap_does_not_bind() -> None:
    spread = {isin: f"S{i}" for i, isin in enumerate(NAMES)}
    s, _ = _replay(D13_RANK_ORDERED_BUYS, _RANKING, spread)
    r, _ = _replay(D13_SECTOR_REFILL, _RANKING, spread)
    assert s.digest() == r.digest()


# ── property: no generated stream ends a sector above the cap ────────────────────────────────────


@settings(max_examples=25, deadline=None)
@given(
    ranking=st.permutations(NAMES),
    labels=st.lists(st.sampled_from("ABC"), min_size=len(NAMES), max_size=len(NAMES)),
    top_n=st.integers(min_value=3, max_value=10),
    refill=st.booleans(),
)
def test_no_generated_ranking_and_sector_map_ends_a_sector_above_the_cap(
    ranking: list[str], labels: list[str], top_n: int, refill: bool
) -> None:
    params = replace(
        D13_SECTOR_REFILL if refill else D13_RANK_ORDERED_BUYS, top_n=top_n, sell_band=top_n
    )
    sectors = dict(zip(NAMES, labels, strict=True))
    result, book = _replay(params, tuple(ranking), sectors)
    positions = {p.isin: p.quantity for p in book.positions()}
    total = book.cash + sum((Decimal(q) * PRICE for q in positions.values()), Decimal(0))
    for sector in set(labels):
        value = sum(
            (Decimal(q) * PRICE for isin, q in positions.items() if sectors[isin] == sector),
            Decimal(0),
        )
        assert value <= CAP * total, (sector, value, total)
    if refill and len(set(labels)) == len(NAMES):
        assert _sector_blocks(result) == 0  # replacements always exist: none is ever refused
    assert all(e.decision is not Decision.SELL for e in result.journal)


# ── regression: a name the rebalance sells is never refilled into the same session ───────────────


def test_the_refill_never_buys_a_name_the_same_rebalance_sells() -> None:
    """2026-11-05 campaign crash (DuplicateStagedOrderError, INE805D01034, 2018-03-05).

    A held name ranked outside the sell band is sold by the rebalance. When the sector cap pushed
    the refill below the band, the refill picked that same name back into the basket, and the
    policy staged a SELL and a BUY of one ISIN for one session. Here sector X fills ranks 1-6 and
    the book holds rank 7 (outside a band of 3): the refill must reach past it, never buy it back.
    """
    ranking = (*_X, *NAMES[6:])  # X = ranks 1-6; NAMES[6] is rank 7
    params = replace(D13_SECTOR_REFILL, top_n=3, sell_band=3)
    sector_map = SectorMap(source="test", sha256="test", by_isin=_SECTORS)
    policy = MomentumV2Policy(
        _Data(ranking), params, order_caps=RAILS, sector_of=sector_map.sector_of
    )
    broker = _Broker(cash="900000", holdings=_held_names(NAMES[6]))
    decision = policy.decide(_ctx(_SESSIONS[0], broker))
    isins = [order.isin for order in decision.orders]
    assert len(isins) == len(set(isins)), isins  # one order per ISIN per session
    sells = {o.isin for o in decision.orders if o.side is Side.SELL}
    buys = {o.isin for o in decision.orders if o.side is Side.BUY}
    assert sells == {NAMES[6]}
    assert buys == {_X[0], NAMES[7], NAMES[8]}  # one X name, then the ranks past the sold one


def test_the_refill_with_no_replacement_left_leaves_the_basket_to_the_gate() -> None:
    """Falsified by hypothesis on 2026-11-06: every name in one sector, top_n=3.

    Each refused name was dropped with no replacement, the basket shrank, the survivors' weights
    rose past the cap in turn, and the last name went too: "cannot weight an empty basket". A
    refused name is now only ever replaced; with none left, the refill behaves as S does.
    """
    one_sector = dict.fromkeys(NAMES, "A")
    params = replace(D13_RANK_ORDERED_BUYS, top_n=3, sell_band=3)
    s_result, s_book = _replay(params, NAMES, one_sector)
    r_result, r_book = _replay(replace(params, sector_refill=True), NAMES, one_sector)
    assert _held(r_book) == _held(s_book)
    assert r_result.digest() == s_result.digest()
