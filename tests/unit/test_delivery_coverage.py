"""D7 delivery coverage: per-session share of price rows carrying a delivery figure.

Written through the real L1 writer into a `tmp_path` lake, so the reader is tested against the
partitions `store/l1.py` actually produces rather than a hand-built parquet that could drift.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.identity.master import Exchange, IdentityMaster
from dataplatform.ingest.models import PriceRow
from dataplatform.ingest.nse.delivery import DeliveryRow
from dataplatform.quality.delivery_coverage import (
    COVERAGE_CHECK_NAME,
    EXCEEDS_CHECK_NAME,
    DeliveryCoverage,
    coverage_by_year,
    coverage_findings,
    main,
    read_delivery_coverage,
    read_quarantine_reasons,
    render,
)
from dataplatform.store.l1 import write_prices_raw

D1 = date(2019, 6, 17)
D2 = date(2020, 1, 2)
ISINS = ("INE002A01018", "INE009A01021", "INE237A01028", "INE296A01016")


def _price(symbol: str, isin: str, day: date, *, series: str = "EQ", qty: int = 100) -> PriceRow:
    one = Decimal("1")
    return PriceRow(
        isin=isin,
        symbol=symbol,
        series=series,
        trade_date=day,
        open=one,
        high=one,
        low=one,
        close=one,
        last=one,
        prev_close=one,
        total_traded_qty=qty,
        total_traded_value=Decimal(qty),
        total_trades=1,
    )


def _deliv(symbol: str, day: date, qty: int, *, series: str = "EQ") -> DeliveryRow:
    return DeliveryRow(
        symbol=symbol, series=series, trade_date=day, deliv_qty=qty, deliv_pct=Decimal("50")
    )


def _lake(root: Path) -> None:
    """D1: 4 EQ rows, 3 with delivery, one of them above its traded qty; one BE row (out of scope)
    and one delivery row for a symbol nothing identifies (quarantined). D2: 2 EQ rows, both full."""
    master = IdentityMaster(())
    write_prices_raw(
        [
            _price("A", ISINS[0], D1),
            _price("B", ISINS[1], D1),
            _price("C", ISINS[2], D1, qty=10),
            _price("D", ISINS[3], D1),
            _price("E", "INE019A01020", D1, series="BE"),
        ],
        delivery_rows=[
            _deliv("A", D1, 60),
            _deliv("B", D1, 40),
            _deliv("C", D1, 25),
            _deliv("GHOST", D1, 1),
        ],
        master=master,
        data_root=root,
    )
    write_prices_raw(
        [_price("A", ISINS[0], D2), _price("B", ISINS[1], D2)],
        delivery_rows=[_deliv("A", D2, 1), _deliv("B", D2, 2)],
        master=master,
        data_root=root,
    )


def test_coverage_is_counted_per_session_over_eq_only(tmp_path: Path) -> None:
    _lake(tmp_path)
    coverage = read_delivery_coverage(date(2019, 1, 1), date(2020, 12, 31), data_root=tmp_path)

    assert coverage == (
        DeliveryCoverage(D1, "NSE", rows=4, with_delivery=3, exceeds_traded=1),
        DeliveryCoverage(D2, "NSE", rows=2, with_delivery=2, exceeds_traded=0),
    )
    assert coverage[0].pct == Decimal("75.00")
    assert coverage[0].missing == 1
    assert coverage[1].pct == Decimal("100.00")


def test_the_range_bounds_are_honoured_and_an_empty_lake_is_an_answer(tmp_path: Path) -> None:
    assert read_delivery_coverage(D1, D2, data_root=tmp_path) == ()
    _lake(tmp_path)
    assert [c.trade_date for c in read_delivery_coverage(D2, D2, data_root=tmp_path)] == [D2]
    assert read_delivery_coverage(D1, D2, exchange=Exchange.BSE.value, data_root=tmp_path) == ()


def test_quarantine_reasons_split_out_the_eq_like_share(tmp_path: Path) -> None:
    _lake(tmp_path)
    (reason,) = read_quarantine_reasons(D1, D2, data_root=tmp_path)
    assert (reason.reason, reason.rows, reason.eq_like_rows) == ("symbol_unresolved", 1, 1)


def test_a_session_below_the_floor_and_an_impossible_row_are_both_findings() -> None:
    coverage = (
        DeliveryCoverage(D1, "NSE", rows=4, with_delivery=3, exceeds_traded=1),
        DeliveryCoverage(D2, "NSE", rows=2, with_delivery=2, exceeds_traded=0),
    )
    findings = coverage_findings(coverage, floor_pct=Decimal("95"))

    assert [(f.logical_date, f.check_name) for f in findings] == [
        (D1, COVERAGE_CHECK_NAME),
        (D1, EXCEEDS_CHECK_NAME),
    ]
    assert all(f.severity == "WARN" for f in findings)
    assert findings[0].observed_value == Decimal("75.00")
    # At the floor is not below it: the comparison is strict, and inverting it flags D2 too.
    assert coverage_findings(coverage, floor_pct=Decimal("75")) == findings[1:]
    # Fingerprints are stable, so a re-run adds nothing.
    assert coverage_findings(coverage) == coverage_findings(coverage)


def test_per_year_roll_up_and_the_rendered_report(tmp_path: Path) -> None:
    _lake(tmp_path)
    coverage = read_delivery_coverage(D1, D2, data_root=tmp_path)

    years = coverage_by_year(coverage)
    assert [(y.trade_date.year, y.rows, y.with_delivery) for y in years] == [
        (2019, 4, 3),
        (2020, 2, 2),
    ]
    text = render(coverage, read_quarantine_reasons(D1, D2, data_root=tmp_path))
    assert "| 2019 | 4 | 3 | 1 | 25.00 | 1 |" in text
    assert "| symbol_unresolved | 1 | 1 |" in text


def test_the_cli_prints_the_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _lake(tmp_path)
    args = ["--from", D1.isoformat(), "--to", D2.isoformat(), "--data-root", str(tmp_path)]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "| 2020 | 2 | 2 | 0 | 0.00 | 0 |" in out
    assert "1 of 2 session(s) below the 95% floor" in out
