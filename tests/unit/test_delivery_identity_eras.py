"""The delivery join across all three format eras, against a master shaped like production's.

The 2026-10-05 audit found ~28 % of NSE EQ `deliv_qty` missing from L1 for two identity reasons,
and both are reproduced here from real files rather than described:

* **Reissued ISINs (`no_matching_price`).** The master is built from `EQUITY_L.csv`, which lists a
  security under today's ISIN from its original listing date — KOTAKBANK as INE237A01036 since
  1995. Every bhavcopy before the 2026-01-14 reissue keys KOTAKBANK's price on INE237A01028, so a
  delivery row resolved through the window landed on no price row. The master now walks
  `isin_lineage` to the ISIN in force that session.
* **ETFs and other instruments the equity list omits (`symbol_unresolved`).** BANKBEES, NIFTYBEES
  and GOLDBEES are in no `EQUITY_L.csv`; the same session's bhavcopy states their ISIN. The
  delivery resolver now asks that statement when the master has nothing.

Fixtures are real and paired by session, one pair per era (`tests/fixtures/*/PROVENANCE.md`):
legacy bhavcopy + MTO (2011-06-22), legacy bhavcopy + `sec_bhavdata_full` (2020-07-13), UDiFF
bhavcopy + `sec_bhavdata_full` (2026-08-07). The master is the real 2026-08-08 `EQUITY_L.csv` and
`symbolchange.csv`, derived by the production `derive_master`. Each fix has a test that fails when
it is switched off, so neither can regress silently.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest

from dataplatform.identity.ingest import derive_master, parse_equity_list, parse_symbol_changes
from dataplatform.identity.master import Exchange, IdentityMaster
from dataplatform.ingest.models import BhavcopyParse
from dataplatform.ingest.nse import bhavcopy, delivery, mto
from dataplatform.ingest.nse.delivery import DeliveryRow
from dataplatform.store.l1 import PricesRawWriteReport, write_prices_raw
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_SCHEMA

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
EQUITY_LIST: Final = FIXTURES / "nse_equity_list" / "2026-08-08"

#: `isin_lineage` edges as the L1-contiguity derivation wrote them (2026-09-29), for the two
#: issuers these fixtures exercise: KOTAKBANK's single reissue and BAJFINANCE's chain of two.
REISSUES: Final = (
    ("INE237A01028", "INE237A01036", date(2026, 1, 14)),
    ("INE296A01016", "INE296A01024", date(2016, 9, 9)),
    ("INE296A01024", "INE296A01032", date(2025, 6, 16)),
)

Parser = Callable[..., tuple[DeliveryRow, ...]]


@dataclass(frozen=True)
class Era:
    name: str
    session: date
    bhavcopy: Path
    delivery: Path
    parse: Parser
    #: ISINs the session's own bhavcopy keys these names on — the answer the join must reach.
    kotak: str
    bajaj: str
    etfs: tuple[tuple[str, str], ...]


ERAS: Final = (
    Era(
        "legacy+mto",
        date(2011, 6, 22),
        FIXTURES / "nse_bhavcopy/legacy/cm22JUN2011bhav.csv.zip",
        FIXTURES / "nse_mto/MTO_22062011.DAT",
        mto.parse,
        kotak="INE237A01028",
        bajaj="INE296A01016",
        etfs=(("NIFTYBEES", "INF732E01011"), ("GOLDBEES", "INF732E01102")),
    ),
    Era(
        "legacy+sec_bhavdata",
        date(2020, 7, 13),
        FIXTURES / "nse_bhavcopy/legacy/cm13JUL2020bhav.csv.zip",
        FIXTURES / "nse_delivery/sec_bhavdata_full_13072020.csv",
        delivery.parse,
        kotak="INE237A01028",
        bajaj="INE296A01024",
        etfs=(("NIFTYBEES", "INF204KB14I2"), ("BANKBEES", "INF204KB15I9")),
    ),
    Era(
        "udiff+sec_bhavdata",
        date(2026, 8, 7),
        FIXTURES / "nse_bhavcopy/udiff/BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip",
        FIXTURES / "nse_delivery/sec_bhavdata_full_07082026.csv",
        delivery.parse,
        kotak="INE237A01036",
        bajaj="INE296A01032",
        etfs=(("BANKBEES", "INF204KB15I9"), ("GOLDBEES", "INF204KB17I5")),
    ),
)


def _master(*, reissues: bool) -> IdentityMaster:
    """The production derivation over the real snapshot — current ISINs, listing-date windows."""
    derived = derive_master(
        parse_equity_list((EQUITY_LIST / "EQUITY_L.csv").read_text("utf-8")),
        parse_symbol_changes((EQUITY_LIST / "symbolchange.csv").read_text("utf-8")),
        snapshot_date=date(2026, 8, 8),
    )
    return IdentityMaster(
        derived.windows,
        securities=derived.securities,
        listings=derived.listings,
        reissues=REISSUES if reissues else (),
    )


def _inputs(era: Era) -> tuple[BhavcopyParse, tuple[DeliveryRow, ...]]:
    prices = bhavcopy.parse_report(
        era.bhavcopy.read_bytes(), filename=era.bhavcopy.name, trade_date=era.session
    )
    rows = era.parse(era.delivery.read_bytes(), filename=era.delivery.name, trade_date=era.session)
    return prices, rows


def _write(
    era: Era, root: Path, *, master: IdentityMaster
) -> tuple[PricesRawWriteReport, dict[tuple[str, str], int | None]]:
    prices, rows = _inputs(era)
    report = write_prices_raw(
        list(prices.rows),
        exchange=Exchange.NSE,
        delivery_rows=rows,
        unidentified_rows=prices.refused,
        master=master,
        data_root=root,
    )
    table = pq.read_table(
        l1_partition_path("prices_raw", era.session, data_root=root), schema=PRICES_RAW_SCHEMA
    ).to_pylist()
    return report, {(str(r["isin"]), str(r["series"])): r["deliv_qty"] for r in table}


def _delivered(era: Era, symbol: str) -> int | None:
    _, rows = _inputs(era)
    (row,) = [r for r in rows if r.symbol == symbol and r.series == "EQ"]
    return row.deliv_qty


@pytest.fixture(scope="module")
def fixed() -> IdentityMaster:
    return _master(reissues=True)


@pytest.mark.parametrize("era", ERAS, ids=lambda era: era.name)
def test_every_delivery_row_lands_on_its_price_row(
    era: Era, fixed: IdentityMaster, tmp_path: Path
) -> None:
    """Nothing the exchange published delivery for is quarantined: all three eras join whole."""
    report, _ = _write(era, tmp_path, master=fixed)

    assert report.delivery_rows > 1000
    assert report.delivery_unresolved == 0
    assert report.delivery_orphaned == 0
    assert report.delivery_joined == report.delivery_rows
    assert report.quarantine_path is None


@pytest.mark.parametrize("era", ERAS, ids=lambda era: era.name)
def test_a_reissued_security_gets_delivery_on_the_isin_its_session_carried(
    era: Era, fixed: IdentityMaster, tmp_path: Path
) -> None:
    """KOTAKBANK before its 2026 reissue, BAJFINANCE across both of its: the in-force ISIN."""
    _, deliv = _write(era, tmp_path, master=fixed)

    assert deliv[(era.kotak, "EQ")] == _delivered(era, "KOTAKBANK")
    assert deliv[(era.bajaj, "EQ")] == _delivered(era, "BAJFINANCE")
    assert deliv[(era.kotak, "EQ")] is not None


@pytest.mark.parametrize("era", ERAS, ids=lambda era: era.name)
def test_the_master_alone_resolves_a_reissued_symbol_to_the_isin_in_force(
    era: Era, fixed: IdentityMaster
) -> None:
    """The lineage half on its own, with no session statement to fall back on."""
    _, rows = _inputs(era)
    resolved = {
        r.symbol: r.isin for r in delivery.resolve(rows, fixed, exchange=Exchange.NSE).resolved
    }

    assert resolved["KOTAKBANK"] == era.kotak
    assert resolved["BAJFINANCE"] == era.bajaj


@pytest.mark.parametrize("era", ERAS[:2], ids=lambda era: era.name)
def test_without_the_lineage_the_master_names_today_s_isin_for_a_past_session(era: Era) -> None:
    """The first defect, kept as a test: the listing-date window alone names the wrong ISIN.

    If `isin_in_force` stopped walking edges (or walked them the wrong way), this is what every
    pre-reissue session would resolve to again — and the test above would fail.
    """
    _, rows = _inputs(era)
    resolved = {
        r.symbol: r.isin
        for r in delivery.resolve(rows, _master(reissues=False), exchange=Exchange.NSE).resolved
    }

    assert resolved["KOTAKBANK"] == "INE237A01036" != era.kotak
    assert resolved["BAJFINANCE"] == "INE296A01032" != era.bajaj


@pytest.mark.parametrize("era", ERAS, ids=lambda era: era.name)
def test_an_etf_the_equity_list_omits_is_placed_by_its_session_s_bhavcopy(
    era: Era, fixed: IdentityMaster, tmp_path: Path
) -> None:
    """No `EQUITY_L.csv` lists an ETF; the session's own bhavcopy names its ISIN."""
    for symbol, _ in era.etfs:
        assert fixed.try_resolve(symbol, era.session) is None, f"{symbol} is not in the master"

    report, deliv = _write(era, tmp_path, master=fixed)

    for symbol, isin in era.etfs:
        assert deliv[(isin, "EQ")] == _delivered(era, symbol), symbol
        assert deliv[(isin, "EQ")] is not None
    assert report.delivery_via_session >= len(era.etfs)


@pytest.mark.parametrize("era", ERAS, ids=lambda era: era.name)
def test_without_the_session_statement_every_etf_is_unresolved(era: Era) -> None:
    """The second defect, kept as a test: the master alone cannot place an ETF's delivery."""
    _, rows = _inputs(era)
    alone = delivery.resolve(rows, _master(reissues=True), exchange=Exchange.NSE)

    unresolved = {row.symbol for row in alone.unresolved}
    for symbol, _ in era.etfs:
        assert symbol in unresolved


def test_the_join_is_byte_identical_when_re_derived(fixed: IdentityMaster, tmp_path: Path) -> None:
    """Idempotence survives the new identity sources: same L0 and master, same bytes."""
    era = ERAS[0]
    _write(era, tmp_path, master=fixed)
    path = l1_partition_path("prices_raw", era.session, data_root=tmp_path)
    first = path.read_bytes()
    _write(era, tmp_path, master=fixed)
    assert path.read_bytes() == first
