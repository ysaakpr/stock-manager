"""X2: the Sec 55(2)(ac) FMV reader over L1 ``prices_raw`` (``backtest.tax_report``).

The lake here is built from scratch under ``tmp_path`` with the real ``PRICES_RAW_SCHEMA`` and no
L2 at all — the FMV is the as-quoted price, so it must resolve without an adjusted store. The
HDFC Bank fixture mirrors the real 31-01-2018 session: NSE quoted the 2011-2019 ISIN, while the
legacy BSE row was resolved to the post-2019 ISIN through today's scrip master.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backtest.tax import (
    InvestorProfile,
    MissingGrandfatheringPriceError,
    PaymentTiming,
    ReissueEvent,
    RunLedger,
    SplitEvent,
    TaxTrade,
    compute_after_tax,
)
from backtest.tax_report import L1GrandfatheringPrices
from backtest.xirr import Cashflow
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA
from execution.broker import Side

FMV_DATE = date(2018, 1, 31)
HDFC_OLD = "INE040A01026"  # face value 2, retired at the 2019 sub-division
HDFC_NEW = "INE040A01034"  # face value 1
LIVE = "INE000L01011"  # untraded on the FMV date, trades again after it
GONE = "INE000G01011"  # last traded before the FMV date, never again (a reissue not carried)
IB_OLD = "INE483S01012"  # Infibeam: 1:10 split ex 31-08-2017 on its last session
IB_NEW = "INE483S01020"  # its successor, first traded 01-09-2017

_Q = Decimal("0.0001")

_PROFILE = InvestorProfile(
    "resident_individual", Decimal("0.3"), Decimal(0), Decimal(0), PaymentTiming.FY_END
)


def _row(
    isin: str, day: date, high: str, *, exchange: str = "NSE", series: str = "EQ"
) -> dict[str, object]:
    h = Decimal(high).quantize(_Q)
    low = (h - Decimal("10")).quantize(_Q)
    return {
        "isin": isin, "exchange": exchange, "symbol": isin[-6:], "series": series,
        "trade_date": day, "open": low, "high": h, "low": low, "close": low, "last": low,
        "prev_close": low, "total_traded_qty": 1000,
        "total_traded_value": (low * 1000).quantize(_Q),
        "total_trades": 10, "deliv_qty": None, "deliv_pct": None,
    }  # fmt: skip


def _write(root: Path, day: date, rows: list[dict[str, object]]) -> None:
    path = l1_partition_path(PRICES_RAW_DATASET, day, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=PRICES_RAW_SCHEMA), path)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    _write(tmp_path, date(2017, 8, 31), [_row(IB_OLD, date(2017, 8, 31), "150")])
    _write(tmp_path, date(2017, 9, 1), [_row(IB_NEW, date(2017, 9, 1), "151")])
    _write(tmp_path, date(2017, 11, 9), [_row(GONE, date(2017, 11, 9), "900")])
    _write(tmp_path, date(2018, 1, 29), [_row(LIVE, date(2018, 1, 29), "51.25")])
    _write(tmp_path, date(2018, 1, 30), [_row(HDFC_OLD, date(2018, 1, 30), "2001")])
    _write(
        tmp_path,
        FMV_DATE,
        [
            _row(HDFC_OLD, FMV_DATE, "2013.5"),
            # A non-EQ series quoting higher: the reader takes EQ only, not the max of every row.
            _row(HDFC_OLD, FMV_DATE, "2100", series="IL"),
            # Same company, same day, pre-split price — filed under the successor ISIN by the
            # legacy BSE resolution. Reading BSE would give a lot on HDFC_NEW a pre-split FMV.
            _row(HDFC_NEW, FMV_DATE, "2011.9", exchange="BSE", series="A"),
            _row(GONE, FMV_DATE, "950", exchange="BSE", series="A"),
            _row(IB_NEW, FMV_DATE, "158.5"),
        ],
    )
    _write(tmp_path, date(2018, 2, 1), [_row(LIVE, date(2018, 2, 1), "60")])
    return tmp_path


def test_retired_isin_held_on_the_fmv_date_reads_its_raw_nse_eq_high(lake: Path) -> None:
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=lake)
    # No L2 exists in this lake: the old ISIN resolves from L1 alone, at the EQ day's high.
    assert fmv.fmv_per_share(HDFC_OLD) == Decimal("2013.5")


def test_bse_row_under_the_successor_isin_is_not_used(lake: Path) -> None:
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=lake)
    with pytest.raises(MissingGrandfatheringPriceError, match="never quoted on NSE EQ"):
        fmv.fmv_per_share(HDFC_NEW)


def test_untraded_on_the_fmv_date_takes_the_last_traded_days_high(lake: Path) -> None:
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=lake)
    # Explanation (a)(ii): 30-01 and 31-01 had sessions without it; 29-01 is the date before.
    assert fmv.fmv_per_share(LIVE) == Decimal("51.25")


def test_isin_that_ceased_before_the_fmv_date_is_explicitly_unavailable(lake: Path) -> None:
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=lake)
    with pytest.raises(MissingGrandfatheringPriceError, match="never after, so it had ceased"):
        fmv.fmv_per_share(GONE)
    with pytest.raises(MissingGrandfatheringPriceError):  # the reason is cached, not re-guessed
        fmv.fmv_per_share(GONE)


def test_missing_fmv_date_partition_is_a_lake_gap_not_a_fallback(tmp_path: Path) -> None:
    _write(tmp_path, date(2018, 1, 30), [_row(HDFC_OLD, date(2018, 1, 30), "2001")])
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=tmp_path)
    with pytest.raises(MissingGrandfatheringPriceError, match="lake gap"):
        fmv.fmv_per_share(HDFC_OLD)


@pytest.mark.parametrize(
    "reissue_day",
    [date(2019, 9, 19), date(2019, 9, 20)],
    ids=["reissue-on-split-day", "reissue-next-session"],
)
def test_hdfc_lot_split_after_the_fmv_date_is_grandfathered_at_raw_fmv_over_gf_units(
    lake: Path, reissue_day: date
) -> None:
    # 100 shares bought 2016 at ₹1,500; the 2019 1:2 sub-division and ISIN reissue make them 200
    # of the new ISIN; sold 2020 at ₹1,200. The lot stays on the old ISIN for its FMV (reissue
    # after 31-01-2018), and gf_units = 2 halves the per-current-share FMV: 2013.5 x 200 / 2.
    # The split is ex on the old ISIN's last session; the book (PR #29) carries the holding on the
    # successor's first session, the next one — both orderings must price the same.
    split_day = date(2019, 9, 19)
    run = RunLedger(
        source="test",
        trades=(
            TaxTrade(
                HDFC_OLD, date(2016, 6, 1), Side.BUY, 100, Decimal("150000"), Decimal(0), True
            ),
            TaxTrade(
                HDFC_NEW, date(2020, 6, 1), Side.SELL, 200, Decimal("240000"), Decimal(0), True
            ),
        ),
        external_flows=(Cashflow(date(2016, 6, 1), Decimal("-1000000")),),
        terminal_date=date(2020, 6, 1),
        terminal_nav=Decimal("1100000"),
        terminal_prices={},
        corporate_events=(
            SplitEvent(HDFC_OLD, split_day, 2, 1),
            ReissueEvent(HDFC_NEW, reissue_day, HDFC_OLD),
        ),
    )
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=lake)
    result = compute_after_tax(run, _PROFILE, fmv=fmv)
    (real,) = result.realisations
    assert real.grandfathered
    assert real.cost == Decimal("201350")  # max(150000, min(201350, 240000))
    assert real.gain == Decimal("38650")


def _infibeam_run(*, carried: bool) -> RunLedger:
    # 100 shares bought 2016 at ₹1,00,000; 1:10 split ex 31-08-2017 on the old ISIN; the book
    # carries the 1,000 shares to the successor on 01-09-2017 (PR #29) — or, before it, did not.
    events: tuple[SplitEvent | ReissueEvent, ...] = (SplitEvent(IB_OLD, date(2017, 8, 31), 10, 1),)
    if carried:
        events = (*events, ReissueEvent(IB_NEW, date(2017, 9, 1), IB_OLD))
    held = IB_NEW if carried else IB_OLD
    return RunLedger(
        source="test",
        trades=(
            TaxTrade(IB_OLD, date(2016, 6, 1), Side.BUY, 100, Decimal("100000"), Decimal(0), True),
            TaxTrade(held, date(2019, 6, 3), Side.SELL, 1000, Decimal("200000"), Decimal(0), True),
        ),
        external_flows=(Cashflow(date(2016, 6, 1), Decimal("-1000000")),),
        terminal_date=date(2019, 6, 3),
        terminal_nav=Decimal("1100000"),
        terminal_prices={},
        corporate_events=events,
    )


def test_lot_carried_to_its_successor_before_the_fmv_date_is_priced_on_the_successor(
    lake: Path,
) -> None:
    # The carry (01-09-2017) precedes 31-01-2018, so the lot's FMV ISIN is the successor's, at its
    # own as-quoted high: 158.5 x 1,000 shares (the split was before the FMV date: gf_units 1).
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=lake)
    (real,) = compute_after_tax(_infibeam_run(carried=True), _PROFILE, fmv=fmv).realisations
    assert real.grandfathered
    assert real.cost == Decimal("158500")


def test_lot_stranded_on_a_retired_isin_is_unavailable_not_priced_off_its_last_quote(
    lake: Path,
) -> None:
    # The pre-#29 ledger shape: the split rescaled the lot but nothing carried it, so it still
    # names an ISIN that last traded 31-08-2017. Its 150 is a pre-split quote on a post-split count
    # — exactly the guess the reader refuses.
    fmv = L1GrandfatheringPrices(fmv_date=FMV_DATE, data_root=lake)
    with pytest.raises(MissingGrandfatheringPriceError, match="ceased before 2018-01-31"):
        compute_after_tax(_infibeam_run(carried=False), _PROFILE, fmv=fmv)
