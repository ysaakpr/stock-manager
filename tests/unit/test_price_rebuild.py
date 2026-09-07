"""The offline price rebuild: fill `prices_raw`'s delivery columns from L0, with no request.

The situation under test is the server's: a `prices_raw` partition written from the bhavcopy alone,
so both delivery columns are NULL on every row, with the delivery payload sitting in L0 all along.
Each test therefore builds the partition the *wrong* way first — through the price source set — and
then asserts what the rebuild does to it.

Real files on both sides, the genuine NSE bhavcopy and the genuine `sec_bhavdata_full` for the same
session (2026-08-07), reusing `test_delivery_backfill`'s fixtures for the reason that file states:
two synthetic files built to agree with each other prove only that they agree.

No network anywhere — the rebuilder has no fetcher to hold — and no database: the identity master is
constructed in the test, as the store would return it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import IdentityMaster
from dataplatform.ingest.backfill import NSE_BHAVCOPY, SOURCE_SETS, WriteContext
from dataplatform.ingest.nse import bhavcopy, delivery
from dataplatform.ingest.price_rebuild import PriceRebuilder
from dataplatform.ingest.source_register import load as load_register
from dataplatform.store.l0 import L0Store
from dataplatform.store.paths import l1_partition_path
from tests.unit.test_delivery_backfill import (
    BHAVCOPY_FIXTURE,
    DELIVERY_FIXTURE,
    SESSION,
)
from tests.unit.test_delivery_backfill import master as master_fixture  # noqa: F401

#: The two columns the whole module is about.
DELIVERY_COLUMNS: Final = ("deliv_qty", "deliv_pct")


@pytest.fixture
def lake(tmp_path: Path) -> L0Store:
    """An L0 lake holding the session's bhavcopy *and* its delivery file — the server's L0."""
    store = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    store.put(
        "nse_bhavcopy_udiff",
        SESSION,
        BHAVCOPY_FIXTURE.name,
        BHAVCOPY_FIXTURE.read_bytes(),
        content_type="application/zip",
    )
    store.put(
        "nse_sec_bhavdata_full",
        SESSION,
        DELIVERY_FIXTURE.name,
        DELIVERY_FIXTURE.read_bytes(),
        content_type="text/csv",
    )
    return store


@pytest.fixture
def master(master_fixture: IdentityMaster) -> IdentityMaster:  # noqa: F811
    """The bhavcopy's own symbol→ISIN map, as `test_delivery_backfill` builds it."""
    return master_fixture


def _ctx(lake: L0Store, master: IdentityMaster) -> WriteContext:
    return WriteContext(l0=lake, data_root=lake.data_root, master=master, register=load_register())


def _write_prices_only(lake: L0Store, master: IdentityMaster) -> None:
    """Land the session the way a price-only backfill did — delivery NULL on every row."""
    ref = lake.ref_for("nse_bhavcopy_udiff", SESSION, BHAVCOPY_FIXTURE.name)
    parsed = bhavcopy.parse_l0_report(lake, ref)
    SOURCE_SETS[NSE_BHAVCOPY].write(parsed, _ctx(lake, master))


def _partition(lake: L0Store) -> Path:
    return l1_partition_path("prices_raw", SESSION, data_root=lake.data_root)


def _delivery_populated(lake: L0Store) -> int:
    """How many rows in the partition carry a delivery percentage."""
    table = pq.read_table(_partition(lake))
    return sum(1 for value in table.column("deliv_pct").to_pylist() if value is not None)


def _rebuilder(lake: L0Store, master: IdentityMaster) -> PriceRebuilder:
    return PriceRebuilder(
        l0=lake, register=load_register(), master=master, data_root=lake.data_root
    )


def test_the_rebuild_fills_delivery_the_price_write_left_null(
    lake: L0Store, master: IdentityMaster
) -> None:
    """The partition starts with no delivery and ends with it, from L0 alone.

    This is the whole point of the module: the delivery join happens at write time, so a partition
    written from the bhavcopy alone cannot be patched — it has to be built again from both inputs.
    A test that fails if the rebuild stops re-deriving, or writes the price side without the join.
    """
    _write_prices_only(lake, master)
    assert _delivery_populated(lake) == 0

    report = _rebuilder(lake, master).run([SESSION])

    assert report.rebuilt == 1
    assert report.no_payload == 0
    assert report.failed == 0
    assert report.delivery_joined > 0

    # Every populated `deliv_pct` came from a joined row, and the shortfall is exactly the rows
    # whose file stated `-` rather than a number: the trade-to-trade series do not report delivery,
    # and `-` is modelled as None and never as 0 (a spurious 0 would read as "0% delivered", the
    # strongest possible distribution signal). So the two counts differ, on purpose.
    parsed = delivery.parse(
        DELIVERY_FIXTURE.read_bytes(), filename=DELIVERY_FIXTURE.name, trade_date=SESSION
    )
    not_stated = sum(1 for row in parsed if row.deliv_pct is None)
    assert not_stated > 0
    assert _delivery_populated(lake) == report.delivery_joined - not_stated


def test_no_delivery_row_is_lost_across_the_rebuild(lake: L0Store, master: IdentityMaster) -> None:
    """The run totals reconcile: parsed == joined + unresolved + orphaned.

    The per-partition contract summed over the walk. A rebuild that dropped the rows it could not
    place would look identical in the joined count and silently under-report its own coverage,
    which is the number a backtest's signal strength depends on.
    """
    _write_prices_only(lake, master)
    report = _rebuilder(lake, master).run([SESSION])

    assert report.delivery_rows > 0
    assert (
        report.delivery_joined + report.delivery_unresolved + report.delivery_orphaned
        == report.delivery_rows
    )
    assert 0.0 < report.join_rate <= 1.0


def test_a_session_with_no_l0_payload_is_named_never_fetched(
    tmp_path: Path, master: IdentityMaster
) -> None:
    """A missing payload is a reported hole, not a fetch — the rebuild has no network at all.

    `PriceRebuilder` is constructed without a fetcher, so this asserts the *reporting* half of that
    design: the session is counted in `no_payload` and listed in `missing`, so an operator knows to
    run the backfill for it rather than discovering later that the column is still NULL there.
    """
    bare = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    bare.put(
        "nse_bhavcopy_udiff",
        SESSION,
        BHAVCOPY_FIXTURE.name,
        BHAVCOPY_FIXTURE.read_bytes(),
        content_type="application/zip",
    )  # prices only: no delivery payload anywhere

    report = _rebuilder(bare, master).run([SESSION])

    assert report.no_payload == 1
    assert report.missing == [SESSION]
    assert report.rebuilt == 0
    assert report.failed == 0
    assert not _partition(bare).exists()  # nothing was written


def test_the_rebuild_is_idempotent(lake: L0Store, master: IdentityMaster) -> None:
    """Re-running over the same L0 leaves the partition byte-identical.

    L0 is immutable, so a rebuild is a pure function of it. That makes a re-run safe after an
    interrupted walk — the operator's actual question — and it is why the walk can continue past a
    failed session instead of unwinding.
    """
    _write_prices_only(lake, master)
    _rebuilder(lake, master).run([SESSION])
    first = _partition(lake).read_bytes()

    _rebuilder(lake, master).run([SESSION])

    assert _partition(lake).read_bytes() == first


def test_the_walk_continues_past_an_unreadable_session(
    lake: L0Store, master: IdentityMaster
) -> None:
    """One corrupt payload costs its own session and nothing else.

    A decade-long rebuild that aborted on a single bad file would have to be restarted from the
    beginning. The bad session is counted, its reason recorded, and the good one still lands.
    """
    corrupt_session = date(2026, 8, 6)
    lake.put(
        "nse_sec_bhavdata_full",
        corrupt_session,
        "sec_bhavdata_full_06082026.csv",
        b"not a delivery file at all",
        content_type="text/csv",
    )
    _write_prices_only(lake, master)

    report = _rebuilder(lake, master).run([corrupt_session, SESSION])

    assert report.rebuilt == 1
    assert report.failed == 1
    assert [day for day, _ in report.failures] == [corrupt_session]
    assert _delivery_populated(lake) > 0  # the good session still landed
