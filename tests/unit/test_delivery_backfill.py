"""The delivery source set: fetch a session's delivery file and re-derive its `prices_raw`.

Real files, both sides. The fixtures are the genuine NSE bhavcopy and the genuine
`sec_bhavdata_full` for the *same* session (2026-08-07), so the join under test is the one that
happens in production against real symbols and real ISINs — not two synthetic files built to agree
with each other, which is exactly where a false-DONE hides (`ops/BACKLOG.md`, M7.3).

No network: the payload is put into a `tmp_path` L0 lake directly, which is what the fetcher would
have done. `write_prices_raw` is exercised for real, so the reconciliation contract
(`delivery_rows == joined + unresolved + orphaned`) is asserted on live data rather than asserted
about.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Final, cast

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import FrozenClock
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest.backfill import (
    NSE_DELIVERY,
    SOURCE_SETS,
    WriteContext,
)
from dataplatform.ingest.nse import bhavcopy, delivery
from dataplatform.ingest.nse.delivery import DeliveryRow
from dataplatform.ingest.source_register import load as load_register
from dataplatform.store.l0 import L0Error, L0Store
from dataplatform.store.l1 import PricesRawWriteReport
from dataplatform.store.paths import l1_partition_path

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SESSION: Final = date(2026, 8, 7)
BHAVCOPY_FIXTURE: Final = (
    REPO_ROOT / "tests/fixtures/nse_bhavcopy/udiff/BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip"
)
DELIVERY_FIXTURE: Final = REPO_ROOT / "tests/fixtures/nse_delivery/sec_bhavdata_full_07082026.csv"


@pytest.fixture
def lake(tmp_path: Path) -> L0Store:
    """An L0 lake holding this session's bhavcopy, exactly where the price backfill put it."""
    store = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    store.put(
        "nse_bhavcopy_udiff",
        SESSION,
        BHAVCOPY_FIXTURE.name,
        BHAVCOPY_FIXTURE.read_bytes(),
        content_type="application/zip",
    )
    return store


@pytest.fixture
def master() -> IdentityMaster:
    """A master built from the bhavcopy's own `(symbol, isin)` pairs.

    The bhavcopy carries ISIN natively, so it *is* a statement of that session's symbol→ISIN map —
    which makes it the honest stand-in for D2 here, and resolves precisely the symbols that traded
    that day. A hand-listed master would only prove the join works for the names I remembered.
    """
    rows = bhavcopy.parse(
        BHAVCOPY_FIXTURE.read_bytes(), filename=BHAVCOPY_FIXTURE.name, trade_date=SESSION
    )
    pairs = {(row.symbol, row.isin) for row in rows}
    windows = tuple(
        SymbolWindow(
            exchange=Exchange.NSE, symbol=sym, valid_from=date(2000, 1, 1), valid_to=None, isin=isin
        )
        for sym, isin in sorted(pairs)
    )
    securities = tuple(
        Security(
            isin=isin,
            name=sym,
            primary_exchange=Exchange.NSE,
            status=ListingStatus.ACTIVE,
            first_seen_date=date(2000, 1, 1),
        )
        for sym, isin in sorted({(s, i) for s, i in pairs})
    )
    return IdentityMaster(windows, securities=securities)


def _write(rows: Sequence[DeliveryRow], ctx: WriteContext) -> PricesRawWriteReport:
    """The source set's write step, typed. `SourceSet.write` returns `object` by contract — it is
    generic over targets whose reports differ — so the delivery set's real report is named here."""
    return cast("PricesRawWriteReport", SOURCE_SETS[NSE_DELIVERY].write(rows, ctx))


def _ctx(lake: L0Store, master: IdentityMaster | None) -> WriteContext:
    return WriteContext(l0=lake, data_root=lake.data_root, master=master, register=load_register())


def test_the_request_names_the_session_and_its_own_sync_source() -> None:
    """One era, so the URL is just the dated template — and delivery checkpoints separately."""
    request = SOURCE_SETS[NSE_DELIVERY].build_request(SESSION, load_register())
    assert request.url.endswith("/sec_bhavdata_full_07082026.csv")
    assert request.filename == "sec_bhavdata_full_07082026.csv"
    # Its own sync_state source: a session can have prices and no delivery, and the interlock
    # should be able to ask about each without one source's gap reddening the other.
    assert request.state_source == NSE_DELIVERY
    assert request.fetch_source == delivery.DELIVERY_SOURCE_ID


def test_delivery_lands_on_the_price_rows_it_belongs_to(
    lake: L0Store, master: IdentityMaster
) -> None:
    """The whole point: a re-derived partition carries delivery figures the old one lacked."""
    rows = delivery.parse(DELIVERY_FIXTURE.read_bytes(), filename=DELIVERY_FIXTURE.name)
    report = _write(rows, _ctx(lake, master))

    table = pq.read_table(l1_partition_path("prices_raw", SESSION, data_root=lake.data_root))
    deliv = table.column("deliv_qty").to_pylist()
    populated = sum(x is not None for x in deliv)
    assert populated > 0, "no delivery figure reached the partition"
    # Most of the session's rows should carry one — this is the column that was 100% NULL before.
    assert populated / len(deliv) > 0.5

    # `delivery_joined` counts rows *placed* on a price row, which is not the same as rows carrying
    # a number: NSE writes "-" for securities whose delivery it does not report (non-EQ series
    # mostly), and the parser reads that as absent rather than as zero. So placed >= populated, and
    # conflating the two would either overstate coverage or look like a join defect.
    assert report.delivery_joined >= populated


def test_no_delivery_row_is_dropped(lake: L0Store, master: IdentityMaster) -> None:
    """M1.8's contract, asserted on real data: every row is joined, unresolved or orphaned."""
    rows = delivery.parse(DELIVERY_FIXTURE.read_bytes(), filename=DELIVERY_FIXTURE.name)
    report = _write(rows, _ctx(lake, master))
    assert report.delivery_rows == len(rows)
    assert (
        report.delivery_rows
        == report.delivery_joined + report.delivery_unresolved + report.delivery_orphaned
    )


def test_the_prices_survive_the_re_derivation(lake: L0Store, master: IdentityMaster) -> None:
    """Re-deriving must add two columns, not replace a session's prices with a delivery file."""
    price_rows = bhavcopy.parse(
        BHAVCOPY_FIXTURE.read_bytes(), filename=BHAVCOPY_FIXTURE.name, trade_date=SESSION
    )
    rows = delivery.parse(DELIVERY_FIXTURE.read_bytes(), filename=DELIVERY_FIXTURE.name)
    SOURCE_SETS[NSE_DELIVERY].write(rows, _ctx(lake, master))

    table = pq.read_table(l1_partition_path("prices_raw", SESSION, data_root=lake.data_root))
    assert table.num_rows == len(price_rows)
    assert set(table.column("isin").to_pylist()) == {row.isin for row in price_rows}


def test_a_session_whose_bhavcopy_is_not_in_l0_fails_loudly(
    tmp_path: Path, master: IdentityMaster
) -> None:
    """Refusing beats writing a partition that quietly lost its prices.

    The delivery file alone cannot rebuild a session — it has no ISIN and no OHLC. A run that wrote
    what it could would replace a good price partition with nothing, and the `sync_state` row would
    say PUBLISHED. Failing leaves the partition untouched and the row FAILED, which a later run
    retries.
    """
    empty = L0Store(clock=FrozenClock(SESSION), data_root=tmp_path)
    rows = delivery.parse(DELIVERY_FIXTURE.read_bytes(), filename=DELIVERY_FIXTURE.name)
    with pytest.raises(L0Error):
        SOURCE_SETS[NSE_DELIVERY].write(rows, _ctx(empty, master))
    assert not l1_partition_path("prices_raw", SESSION, data_root=tmp_path).exists()


def test_an_empty_delivery_file_does_not_rewrite_the_partition(
    lake: L0Store, master: IdentityMaster
) -> None:
    """A served-but-empty file is a source failure, not a session with no delivery."""
    with pytest.raises(ValueError, match="zero rows"):
        SOURCE_SETS[NSE_DELIVERY].write((), _ctx(lake, master))


def test_the_join_is_refused_without_the_identity_master(lake: L0Store) -> None:
    """Invariant #2: a delivery row has no ISIN, so symbols may only resolve through D2."""
    rows = delivery.parse(DELIVERY_FIXTURE.read_bytes(), filename=DELIVERY_FIXTURE.name)
    with pytest.raises(ValueError, match="IdentityMaster"):
        SOURCE_SETS[NSE_DELIVERY].write(rows, _ctx(lake, None))


def test_the_era_boundary_picks_the_right_file() -> None:
    """Before 2019-09-30 the modern file 404s; the older MTO report covers those sessions.

    The boundary is in our sourcing, not in the market, so both eras publish under one
    `sync_state` source — the interlock asks one question about delivery coverage across the
    decade rather than two that meet at a seam.
    """
    register = load_register()
    old = SOURCE_SETS[NSE_DELIVERY].build_request(date(2016, 9, 2), register)
    new = SOURCE_SETS[NSE_DELIVERY].build_request(date(2026, 8, 7), register)

    assert old.url.endswith("/archives/equities/mto/MTO_02092016.DAT")
    assert old.fetch_source == "nse_mto"
    assert new.url.endswith("/products/content/sec_bhavdata_full_07082026.csv")
    assert new.fetch_source == delivery.DELIVERY_SOURCE_ID
    assert old.state_source == new.state_source == NSE_DELIVERY

    # The boundary itself belongs to the modern file — it is the first session that archive serves.
    edge = SOURCE_SETS[NSE_DELIVERY].build_request(delivery.SEC_BHAVDATA_ERA_START, register)
    assert edge.fetch_source == delivery.DELIVERY_SOURCE_ID
    before = SOURCE_SETS[NSE_DELIVERY].build_request(date(2019, 9, 27), register)
    assert before.fetch_source == "nse_mto"
