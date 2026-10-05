"""D3/D4 — price-implied splits: the share-basis changes no corporate-action feed published.

Both CA feeds cover listed equity only, so an ETF unit split is in neither: GOLDBEES
(INF732E01102) closed 3,359.60 on 2019-12-18 and 33.55 on 2019-12-19, and L2 showed a -99% day.
`corpactions.implied` reads such a step off L1; `store.l2.materialize_isin` composes it into the
chain it writes. These tests pin the evidence rules down from both sides — every true positive
the audit found must be caught, and each look-alike (a crash, a suspension, a demerger, a recorded
split a day off) must not be — and they fail if the factor is applied the wrong way round.

Offline and deterministic: synthetic bars, synthetic L1 under `tmp_path`, no Postgres.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from dataplatform.corpactions.factors import FactorChain, build_chain_for_isin, with_price_events
from dataplatform.corpactions.implied import (
    IMPLIED_SOURCE,
    SessionBar,
    detect_implied_splits,
)
from dataplatform.corpactions.taxonomy import (
    ActionType,
    DividendKind,
    DividendTerms,
    FaceValueTerms,
    RatioTerms,
    UnquantifiedTerms,
)
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.store.db import Connection
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    materialize_isin,
    materialized_isins,
    prune_retired,
    read_adjusted,
    rebuild_all,
    rebuild_invalidated,
)
from dataplatform.store.paths import l2_isin_partition_path

GOLDBEES = "INF732E01102"
STOCK = "INE002A01018"
EX = date(2019, 12, 19)


def _bars(
    closes: list[tuple[str, str]], *, volumes: list[int], start: date = date(2019, 12, 2)
) -> list[SessionBar]:
    """Weekday sessions from `start`: `(open, close)` per session with its traded quantity."""
    out: list[SessionBar] = []
    day = start
    for (o, c), v in zip(closes, volumes, strict=True):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append(SessionBar(trade_date=day, open=Decimal(o), close=Decimal(c), volume=Decimal(v)))
        day += timedelta(days=1)
    return out


def _flat_then(
    step: tuple[str, str], *, level: str, n: int = 12, vol: int = 5_000, step_vol: int
) -> list[SessionBar]:
    """`n` sessions at `level` with quantity `vol`, then one `step` session with `step_vol`."""
    closes = [(level, level)] * n + [step]
    return _bars(closes, volumes=[vol] * n + [step_vol])


def _action(
    isin: str, ex: date, kind: ActionType, terms: object, *, source: str = "nse_corp_actions"
) -> CorporateAction:
    return CorporateAction(
        isin=isin,
        ex_date=ex,
        action_type=kind,
        terms=terms,  # type: ignore[arg-type]
        source=source,
        raw_text="test",
        knowable_date=ex,
    )


# ── detection: what is a share-basis change and what is not ─────────────────────────────────


def test_an_etf_unit_split_is_implied_from_price_and_volume() -> None:
    # GOLDBEES, 2019-12-19: 3,359.60 → open 30.20 / close 33.55, quantity ~5k → 1.66M.
    bars = _flat_then(("30.20", "33.55"), level="3359.60", step_vol=1_664_422)
    [event] = detect_implied_splits(GOLDBEES, bars)
    assert event.ex_date == bars[-1].trade_date
    assert (event.from_value, event.to_value) == (Decimal(100), Decimal(1))
    assert event.price_factor == Decimal("0.01")
    action = event.as_action()
    assert action.source == IMPLIED_SOURCE
    assert action.action_type is ActionType.SPLIT
    assert action.knowable_date == event.ex_date  # knowable no earlier than the session showing it


def test_a_split_day_that_also_moved_is_still_caught_at_5x() -> None:
    # TATAMTRDVR 2011-09-12: 448.45 → 85.75 / 85.05 (5.2x and 5.3x: the split day fell ~5% too).
    bars = _flat_then(("85.75", "85.05"), level="448.45", vol=196_623, step_vol=989_147)
    [event] = detect_implied_splits("IN9155A01012", bars)
    assert event.price_factor == Decimal("0.2")


def test_a_halving_without_the_volume_moving_is_not_a_split() -> None:
    # A crash (or an unrecorded demerger) halves the price; the share count is unchanged.
    bars = _flat_then(("250.00", "250.00"), level="500.00", step_vol=5_000)
    assert detect_implied_splits(STOCK, bars) == ()


def test_a_step_that_is_no_clean_multiple_is_not_a_split() -> None:
    bars = _flat_then(("140.00", "140.00"), level="500.00", step_vol=50_000)  # 3.57x
    assert detect_implied_splits(STOCK, bars) == ()


def test_one_bad_print_cannot_make_a_split() -> None:
    # The close is exactly /10, but the open says nothing happened: one ratio is not evidence.
    bars = _flat_then(("495.00", "50.00"), level="500.00", step_vol=50_000)
    assert detect_implied_splits(STOCK, bars) == ()


def test_a_tick_bounce_on_a_penny_name_is_not_a_split() -> None:
    # INE890I01035, 2016-10-03: 0.10 → 0.05, quantity 6k → 342k — exactly 2x, and exactly a tick.
    bars = _flat_then(("0.05", "0.05"), level="0.10", step_vol=342_412)
    assert detect_implied_splits(STOCK, bars) == ()


def test_a_step_that_reverts_within_days_is_not_a_split() -> None:
    bars = _flat_then(("50.00", "50.00"), level="500.00", step_vol=50_000)
    back = SessionBar(
        trade_date=bars[-1].trade_date + timedelta(days=1),
        open=Decimal(498),
        close=Decimal(499),
        volume=Decimal(5_000),
    )
    assert detect_implied_splits(STOCK, [*bars, back]) == ()


def test_a_step_across_a_long_gap_is_never_implied() -> None:
    # A suspension: months without a bar, then a tenth of the price. Genuine moves happen there
    # (DOLPHIN, UEL); the quality check classifies them, detection must not adjust them.
    bars = _flat_then(("50.00", "50.00"), level="500.00", step_vol=50_000)
    late = SessionBar(
        trade_date=bars[-2].trade_date + timedelta(days=6),
        open=bars[-1].open,
        close=bars[-1].close,
        volume=bars[-1].volume,
    )
    assert detect_implied_splits(STOCK, [*bars[:-1], late]) == ()


def test_a_consolidation_is_implied_the_other_way_round() -> None:
    bars = _flat_then(("50.00", "50.50"), level="5.00", vol=100_000, step_vol=9_000)
    [event] = detect_implied_splits(STOCK, bars)
    assert (event.from_value, event.to_value) == (Decimal(1), Decimal(10))
    assert event.price_factor == Decimal(10)


def test_a_recorded_structural_break_explains_the_step() -> None:
    bars = _flat_then(("100.00", "100.00"), level="300.00", step_vol=60_000)
    demerger = _action(STOCK, bars[-1].trade_date, ActionType.DEMERGER, UnquantifiedTerms())
    assert detect_implied_splits(STOCK, bars, [demerger]) == ()


def test_a_large_recorded_dividend_explains_the_step() -> None:
    # MAJESCO 2020-12-23: ₹974 on a ₹985 share.
    bars = _flat_then(("12.20", "12.20"), level="985.65", step_vol=80_000)
    dividend = _action(
        STOCK,
        bars[-1].trade_date,
        ActionType.DIVIDEND,
        DividendTerms(amount_inr=Decimal(974), dividend_kind=DividendKind.INTERIM),
    )
    assert detect_implied_splits(STOCK, bars, [dividend]) == ()


def test_a_recorded_split_a_day_off_is_not_adjusted_twice() -> None:
    bars = _flat_then(("50.00", "50.00"), level="500.00", step_vol=50_000)
    recorded = _action(
        STOCK,
        bars[-1].trade_date + timedelta(days=1),
        ActionType.SPLIT,
        FaceValueTerms(from_value=Decimal(10), to_value=Decimal(1)),
    )
    assert detect_implied_splits(STOCK, bars, [recorded]) == ()


def test_a_second_unpublished_event_on_a_recorded_day_is_caught() -> None:
    # INE096L01025 2016-08-11: a recorded 1:25 bonus, and the price halved. In the recorded
    # chain's terms the step left is 2x — the unpublished split that came with it.
    level = Decimal("542.90") * Decimal(25) / Decimal(26)  # pre-ex close in post-bonus terms
    bars = _flat_then(("268.00", "266.50"), level=str(level), step_vol=20_000)
    bonus = _action(
        STOCK,
        bars[-1].trade_date,
        ActionType.BONUS,
        RatioTerms(new_shares=Decimal(1), held_shares=Decimal(25)),
    )
    [event] = detect_implied_splits(STOCK, bars, [bonus])
    assert event.price_factor == Decimal("0.5")


# ── the chain: an implied event composes like a published one ───────────────────────────────


def test_with_price_events_composes_and_refolds_the_cumulative_factor() -> None:
    bonus = _action(
        STOCK,
        date(2024, 1, 10),
        ActionType.BONUS,
        RatioTerms(new_shares=Decimal(1), held_shares=Decimal(1)),
    )
    chain = build_chain_for_isin(STOCK, [bonus])
    split = _action(
        STOCK,
        date(2020, 6, 1),
        ActionType.SPLIT,
        FaceValueTerms(from_value=Decimal(10), to_value=Decimal(1)),
        source=IMPLIED_SOURCE,
    )
    merged = with_price_events(chain, [split])
    assert [r.ex_date for r in merged.rows] == [date(2020, 6, 1), date(2024, 1, 10)]
    # Before both: 0.1 x 0.5. Inverted (x10 instead of x0.1) this reads 5, not 0.05.
    assert merged.price_factor_asof(date(2020, 5, 29)) == Decimal("0.05")
    assert merged.price_factor_asof(date(2020, 6, 1)) == Decimal("0.5")
    assert merged.price_factor_asof(date(2024, 1, 10)) == Decimal(1)
    assert with_price_events(chain, []) is chain


# ── the materializer: L2 carries the implied factor ─────────────────────────────────────────


def _row(isin: str, day: date, *, o: str, c: str, qty: int, series: str = "EQ") -> PriceRow:
    op, cl = Decimal(o), Decimal(c)
    return PriceRow(
        isin=isin,
        symbol=isin[:6],
        series=series,
        trade_date=day,
        open=op,
        high=max(op, cl),
        low=min(op, cl),
        close=cl,
        last=cl,
        prev_close=cl,
        total_traded_qty=qty,
        total_traded_value=cl * qty,
        total_trades=10,
    )


def _write_goldbees(root: Path, isin: str = GOLDBEES) -> list[date]:
    days = [d.trade_date for d in _flat_then(("30.20", "33.55"), level="1", step_vol=1)]
    for day in days[:-1]:
        write_prices_raw(
            [_row(isin, day, o="3359.60", c="3359.60", qty=4_386)],
            exchange=Exchange.NSE,
            data_root=root,
        )
    write_prices_raw(
        [_row(isin, days[-1], o="30.20", c="33.55", qty=1_664_422)],
        exchange=Exchange.NSE,
        data_root=root,
    )
    return days


def test_l2_adjusts_an_implied_split_and_keeps_adj_equal_raw_times_factor(tmp_path: Path) -> None:
    days = _write_goldbees(tmp_path)
    report = materialize_isin(
        GOLDBEES, chain=FactorChain(isin=GOLDBEES, rows=()), actions=(), data_root=tmp_path
    )
    assert [s.ex_date for s in report.implied_splits] == [days[-1]]
    bars = read_adjusted(GOLDBEES, data_root=tmp_path)
    # Hand-computed: 3359.60 x 0.01 = 33.5960 before the ex-date; 33.55 x 1 on it. Inverted
    # (x100) the pre-ex close would read 335960.0000.
    assert bars[-2].adj_close == Decimal("33.5960")
    assert bars[-2].cum_price_factor == Decimal("0.01")
    assert bars[-1].adj_close == Decimal("33.5500")
    # Volume back-adjusts with the reciprocal: 4,386 units then are 438,600 units now.
    assert bars[-2].adj_volume == Decimal(438_600)
    # The total-return leg sees the same event — it must not keep the -99% day either.
    assert bars[-2].tr_close == bars[-2].adj_close


def test_the_detector_can_be_turned_off(tmp_path: Path) -> None:
    _write_goldbees(tmp_path)
    report = materialize_isin(
        GOLDBEES,
        chain=FactorChain(isin=GOLDBEES, rows=()),
        actions=(),
        data_root=tmp_path,
        infer_splits=False,
    )
    assert report.implied_splits == ()
    assert read_adjusted(GOLDBEES, data_root=tmp_path)[-2].adj_close == Decimal("3359.6000")


# ── retired partitions: pruned, never rebuilt ───────────────────────────────────────────────

RETIRED = "INE296A01016"  # BAJFINANCE before its 2016 reissue
SURVIVOR = "INE296A01024"


def _survivor_of(isin: str) -> str:
    return SURVIVOR if isin == RETIRED else isin


class _Store:
    """A stand-in Postgres: no factors, no actions, and an `l2_invalidation` queue of `queued`."""

    def __init__(self, queued: tuple[str, ...] = ()) -> None:
        self.queued = queued
        self.resolved: list[str] = []
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: object = None) -> _Store:
        self._rows = []
        if sql.startswith("SELECT DISTINCT isin FROM l2_invalidation"):
            self._rows = [(i,) for i in self.queued]
        elif sql.startswith("UPDATE l2_invalidation"):
            assert isinstance(params, tuple)
            self.resolved.append(str(params[1]))
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


def _write_pair(root: Path) -> None:
    # One L1 write per (exchange, date) partition: a second write to the same date replaces it.
    day = date(2016, 9, 7)
    write_prices_raw(
        [_row(isin, day, o="100", c="100", qty=10) for isin in (RETIRED, SURVIVOR)],
        exchange=Exchange.NSE,
        data_root=root,
    )
    for isin in (RETIRED, SURVIVOR):
        materialize_isin(isin, chain=FactorChain(isin=isin, rows=()), actions=(), data_root=root)


def test_prune_removes_only_retired_partitions_and_is_idempotent(tmp_path: Path) -> None:
    _write_pair(tmp_path)
    assert prune_retired(_survivor_of, data_root=tmp_path) == (RETIRED,)
    assert materialized_isins(data_root=tmp_path) == frozenset({SURVIVOR})
    assert prune_retired(_survivor_of, data_root=tmp_path) == ()


def test_the_queue_resolves_a_retired_isin_without_building_it(tmp_path: Path) -> None:
    from dataplatform.clock import FrozenClock

    _write_pair(tmp_path)
    store = _Store(queued=(RETIRED, SURVIVOR))
    reports = rebuild_invalidated(
        cast(Connection, store),
        clock=FrozenClock(date(2026, 10, 5)),
        data_root=tmp_path,
        survivor_of=_survivor_of,
    )
    assert [r.isin for r in reports] == [SURVIVOR]
    assert sorted(store.resolved) == sorted((RETIRED, SURVIVOR))
    assert not l2_isin_partition_path(PRICES_ADJUSTED_DATASET, RETIRED, data_root=tmp_path).exists()


def test_rebuild_all_matches_a_fresh_build_and_prunes(tmp_path: Path) -> None:
    _write_pair(tmp_path)
    _write_goldbees(tmp_path)
    report = rebuild_all(cast(Connection, _Store()), data_root=tmp_path, survivor_of=_survivor_of)
    assert report.pruned_retired == (RETIRED,)
    assert report.skipped_retired == 1
    assert sorted(r.isin for r in report.written) == sorted((GOLDBEES, SURVIVOR))
    assert len(report.implied_splits) == 1
    first = {
        i: l2_isin_partition_path(PRICES_ADJUSTED_DATASET, i, data_root=tmp_path).read_bytes()
        for i in (GOLDBEES, SURVIVOR)
    }
    again = rebuild_all(cast(Connection, _Store()), data_root=tmp_path, survivor_of=_survivor_of)
    assert again.pruned_retired == ()
    for isin, payload in first.items():
        path = l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=tmp_path)
        assert path.read_bytes() == payload


@pytest.mark.parametrize("batch_size", [1, 2, 500])
def test_rebuild_all_does_not_depend_on_the_batch(tmp_path: Path, batch_size: int) -> None:
    _write_pair(tmp_path)
    _write_goldbees(tmp_path)
    report = rebuild_all(
        cast(Connection, _Store()),
        data_root=tmp_path,
        survivor_of=_survivor_of,
        batch_size=batch_size,
    )
    assert sorted(r.isin for r in report.written) == sorted((GOLDBEES, SURVIVOR))
    assert read_adjusted(GOLDBEES, data_root=tmp_path)[-2].adj_close == Decimal("33.5960")
