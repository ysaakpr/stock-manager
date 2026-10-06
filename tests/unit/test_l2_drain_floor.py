"""M13.8 — draining the L2 invalidation queue never extends an ISIN's history.

L1 reaches back to 2006; L2 partitions start at 2011-06-22. `rebuild_invalidated` used to rebuild
each queued ISIN over its *full* L1 history, so the weekly `ca_refresh` drain (3,452 queued ISINs,
~1,126 with pre-2011 bars) would have silently pulled uncurated 2006-2011 steps into L2 and turned
`l2_continuity` red. Extension is `l2_fill --extend`'s job alone.

These tests pin the floor: a partition that exists keeps its first date across a drain, lineage
stitching respects it, a brand-new ISIN is built as the first-time fill builds it, and `--extend`
still reaches back. The fixture puts a 10:1 unrecorded price step *before* the floor, so a drain
that dropped or inverted the floor shows up as a wrong first date and a wrong row count.

Offline and deterministic: synthetic rows under `tmp_path`, a fake queue store, no network.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import FrozenClock
from dataplatform.corpactions import ActionType, FaceValueTerms, build_chain_for_isin
from dataplatform.corpactions.factors import FactorChain
from dataplatform.identity import lineage_rebuild
from dataplatform.identity.master import Exchange
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.store.db import Connection
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import (
    PRICES_ADJUSTED_DATASET,
    materialize_isin,
    materialize_missing,
    read_adjusted,
    rebuild_invalidated,
    rebuild_truncated,
)
from dataplatform.store.paths import l2_isin_partition_path

OLD = "INE002A01018"  # partition on disk from FLOOR; L1 backfilled to 2006 underneath it
NEW = "INE009A01021"  # queued, L1 back to 2006, no partition yet
RETIRED = "INE040A01034"  # OLD's pre-reissue ISIN in the lineage case

PRE_FLOOR = (date(2006, 3, 1), date(2006, 3, 2), date(2011, 6, 21))
FLOOR = date(2011, 6, 22)
POST_FLOOR = (FLOOR, date(2011, 6, 23), date(2011, 6, 24))


class _QueueStore:
    """Stands in for Postgres: an `l2_invalidation` queue, no factors, no reconciled actions."""

    def __init__(self, queued: tuple[str, ...]) -> None:
        self.queued = queued
        self.resolved: list[str] = []
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> _QueueStore:
        if "FROM l2_invalidation" in sql and sql.lstrip().startswith("SELECT"):
            self._rows = [(isin,) for isin in sorted(self.queued)]
        elif sql.lstrip().startswith("UPDATE l2_invalidation"):
            assert params is not None
            self.resolved.append(str(params[1]))
            self._rows = []
        else:
            self._rows = []
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


def _row(isin: str, day: date, close: str) -> PriceRow:
    price = Decimal(close)
    return PriceRow(
        isin=isin,
        symbol=isin[:6],
        series="EQ",
        trade_date=day,
        open=price,
        high=price,
        low=price,
        close=price,
        last=price,
        prev_close=price,
        total_traded_qty=1_000,
        total_traded_value=price * 1_000,
        total_trades=10,
    )


def _write(root: Path, isins: tuple[str, ...], days: tuple[date, ...], close: str) -> None:
    for day in days:
        write_prices_raw(
            [_row(isin, day, close) for isin in isins], exchange=Exchange.NSE, data_root=root
        )


def _part(root: Path, isin: str) -> Path:
    return l2_isin_partition_path(PRICES_ADJUSTED_DATASET, isin, data_root=root)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """OLD's partition built when L1 began at FLOOR; then L1 was backfilled to 2006 under it.

    The pre-floor bars sit at 10x the post-floor level — an uncurated step no factor explains,
    exactly what the drain must not surface.
    """
    _write(tmp_path, (OLD,), POST_FLOOR, "100.00")
    materialize_missing(cast(Connection, _QueueStore(())), data_root=tmp_path)
    # An L1 write replaces its whole date partition, so each session is written with every ISIN.
    _write(tmp_path, (OLD, NEW), PRE_FLOOR, "1000.00")
    _write(tmp_path, (OLD, NEW), POST_FLOOR, "100.00")
    return tmp_path


def _drain(root: Path, queued: tuple[str, ...], **kwargs: object) -> _QueueStore:
    store = _QueueStore(queued)
    rebuild_invalidated(
        cast(Connection, store),
        clock=FrozenClock(date(2026, 10, 10)),
        data_root=root,
        **kwargs,  # type: ignore[arg-type]
    )
    return store


def test_a_queued_isin_with_a_partition_is_rebuilt_from_its_floor_only(lake: Path) -> None:
    assert min(b.trade_date for b in read_adjusted(OLD, data_root=lake)) == FLOOR
    store = _drain(lake, (OLD,))
    bars = read_adjusted(OLD, data_root=lake)
    # Dropping the floor gives 2006-03-01 and six rows; inverting it (keeping only bars before the
    # floor) gives 2011-06-21 and three rows at the 10x level. Both fail here.
    assert min(b.trade_date for b in bars) == FLOOR
    assert sorted(b.trade_date for b in bars) == list(POST_FLOOR)
    assert {b.adj_close for b in bars} == {Decimal("100.00")}
    assert store.resolved == [OLD], "the invalidation must still be resolved"


def test_the_drain_rebuild_is_the_partition_a_floor_era_build_wrote(lake: Path) -> None:
    before = _part(lake, OLD).read_bytes()
    _drain(lake, (OLD,))
    assert _part(lake, OLD).read_bytes() == before


def test_lineage_stitching_respects_the_floor(tmp_path: Path) -> None:
    # OLD's stitched partition was built over RETIRED -> OLD when L1 began at FLOOR; then both
    # ISINs were backfilled to 2006. The rebuild over the chain must not reach back either.
    _write(tmp_path, (RETIRED,), (FLOOR,), "100.00")
    _write(tmp_path, (OLD,), POST_FLOOR[1:], "100.00")
    history = {OLD: (RETIRED, OLD)}
    materialize_missing(
        cast(Connection, _QueueStore(())),
        data_root=tmp_path,
        history_for=history,
        survivor_of=lambda i: OLD if i == RETIRED else i,
    )
    _write(tmp_path, (RETIRED,), PRE_FLOOR, "1000.00")
    _drain(
        tmp_path,
        (OLD,),
        history_for=history,
        survivor_of=lambda i: OLD if i == RETIRED else i,
    )
    bars = read_adjusted(OLD, data_root=tmp_path)
    assert sorted(b.trade_date for b in bars) == list(POST_FLOOR)


def test_a_brand_new_isin_gets_the_first_time_fill_start(lake: Path, tmp_path: Path) -> None:
    _drain(lake, (NEW,))
    drained = read_adjusted(NEW, data_root=lake)
    assert min(b.trade_date for b in drained) == PRE_FLOOR[0]

    # The same start, and the same bytes, `materialize_missing` writes for it.
    fresh = tmp_path / "fresh"
    _write(fresh, (NEW,), PRE_FLOOR, "1000.00")
    _write(fresh, (NEW,), POST_FLOOR, "100.00")
    materialize_missing(cast(Connection, _QueueStore(())), data_root=fresh)
    assert _part(lake, NEW).read_bytes() == _part(fresh, NEW).read_bytes()


def test_extend_still_extends_after_a_drain(lake: Path) -> None:
    _drain(lake, (OLD,))
    report = rebuild_truncated(cast(Connection, _QueueStore(())), data_root=lake)
    assert [r.isin for r in report.written] == [OLD]
    assert report.written[0].from_date == PRE_FLOOR[0]
    assert min(b.trade_date for b in read_adjusted(OLD, data_root=lake)) == PRE_FLOOR[0]


def test_an_explicit_floor_is_what_materialize_isin_honours(lake: Path) -> None:
    chain = FactorChain(isin=NEW, rows=())
    floored = materialize_isin(NEW, chain=chain, actions=(), data_root=lake, history_floor=FLOOR)
    assert (floored.from_date, floored.rows_written) == (FLOOR, len(POST_FLOOR))
    whole = materialize_isin(NEW, chain=chain, actions=(), data_root=lake)
    assert (whole.from_date, whole.rows_written) == (PRE_FLOOR[0], 6)


# ── floor_existing=False: the lineage rebuild's stage 4 stitches the whole history ─────────────


def test_an_unfloored_drain_rebuilds_over_the_full_history(lake: Path) -> None:
    store = _drain(lake, (OLD,), floor_existing=False)
    bars = read_adjusted(OLD, data_root=lake)
    assert min(b.trade_date for b in bars) == PRE_FLOOR[0]
    assert len(bars) == len(PRE_FLOOR) + len(POST_FLOOR)
    assert store.resolved == [OLD]


def test_an_unfloored_drain_stitches_a_newly_derived_predecessor(tmp_path: Path) -> None:
    # OLD's partition was built from its own bars, before the RETIRED -> OLD edge was derived.
    _write(tmp_path, (RETIRED,), PRE_FLOOR, "100.00")
    _write(tmp_path, (OLD,), POST_FLOOR, "100.00")
    materialize_missing(cast(Connection, _QueueStore(())), data_root=tmp_path)
    assert min(b.trade_date for b in read_adjusted(OLD, data_root=tmp_path)) == FLOOR

    history = {OLD: (RETIRED, OLD)}
    retire = {"survivor_of": lambda i: OLD if i == RETIRED else i, "history_for": history}
    _drain(tmp_path, (OLD,), floor_existing=False, **retire)
    bars = read_adjusted(OLD, data_root=tmp_path)
    assert sorted(b.trade_date for b in bars) == [*PRE_FLOOR, *POST_FLOOR]


def test_the_lineage_rebuild_drains_unfloored() -> None:
    # `rebuild()` needs Postgres and a real lake end to end; this pins the one call-site argument
    # that decides whether a newly derived edge stitches anything at all.
    tree = ast.parse(inspect.getsource(lineage_rebuild.rebuild))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "rebuild_invalidated"
    ]
    assert len(calls) == 1
    kwargs = {k.arg: k.value for k in calls[0].keywords}
    floor = kwargs.get("floor_existing")
    assert isinstance(floor, ast.Constant) and floor.value is False


# ── a recorded action before the floor changes nothing after it ───────────────────────────────


def test_a_split_before_the_floor_leaves_post_floor_bars_identical(tmp_path: Path) -> None:
    # A recorded 10 -> 2 face-value split in 2006, well before the floor, and another just after
    # it, so the post-floor cumulative factors are not trivially 1.
    early = date(2006, 3, 2)
    late = POST_FLOOR[2]
    _write(tmp_path, (NEW,), (PRE_FLOOR[0],), "1000.00")
    _write(tmp_path, (NEW,), (early, PRE_FLOOR[2], *POST_FLOOR[:2]), "200.00")
    _write(tmp_path, (NEW,), (late,), "40.00")
    actions = [
        CorporateAction(
            isin=NEW,
            ex_date=ex,
            action_type=ActionType.SPLIT,
            terms=FaceValueTerms(from_value=Decimal(f), to_value=Decimal(t)),
            source="nse_corp_actions",
            raw_text=f"SPLIT {ex.isoformat()}",
            knowable_date=ex,
        )
        for ex, f, t in ((early, "10", "2"), (late, "2", "0.4"))
    ]
    chain = build_chain_for_isin(NEW, actions)

    def post_floor(floor: date | None) -> list[tuple[object, ...]]:
        materialize_isin(NEW, chain=chain, actions=actions, data_root=tmp_path, history_floor=floor)
        return [
            (
                b.trade_date,
                b.adj_open,
                b.adj_high,
                b.adj_low,
                b.adj_close,
                b.adj_volume,
                b.tr_close,
                b.cum_price_factor,
                b.cum_qty_factor,
            )
            for b in read_adjusted(NEW, data_root=tmp_path)
            if b.trade_date >= FLOOR
        ]

    whole = post_floor(None)
    floored = post_floor(FLOOR)
    assert floored == whole
    assert [r[0] for r in floored] == list(POST_FLOOR)
    assert floored[0][7] == Decimal("0.2"), "the post-floor split must still adjust"
    assert {r[4] for r in floored} == {Decimal("40.00")}


# ── a zero-row partition fails loud ──────────────────────────────────────────────────────────


def test_an_empty_partition_raises_instead_of_dropping_the_floor(lake: Path) -> None:
    path = _part(lake, OLD)
    table = pq.read_table(path)
    pq.write_table(table.slice(0, 0), path)
    store = _QueueStore((OLD,))
    with pytest.raises(ValueError, match=f"no rows.*{OLD}"):
        rebuild_invalidated(
            cast(Connection, store), clock=FrozenClock(date(2026, 10, 10)), data_root=lake
        )
    assert store.resolved == [], "nothing may be resolved when the floor cannot be read"
    assert pq.read_table(path).num_rows == 0, "the drain must not have rewritten the partition"


def test_an_empty_partition_does_not_block_an_unfloored_drain(lake: Path) -> None:
    path = _part(lake, OLD)
    pq.write_table(pq.read_table(path).slice(0, 0), path)
    _drain(lake, (OLD,), floor_existing=False)
    assert len(read_adjusted(OLD, data_root=lake)) == len(PRE_FLOOR) + len(POST_FLOOR)
