"""Golden cases for price-implied splits — events no feed published, adjusted by hand (reference B).

The §4.3 cases in `cases/` start from *published* terms; these two have none to start from. Neither
NSE's equity corporate-action feed nor BSE's carries an ETF unit split, and neither reaches back to
TATAMTRDVR's 2011 sub-division, so `corpactions.implied` reads each off L1 and `store.l2` adjusts
for it. The bars below are frozen from L1 (NSE EQ, verbatim); the expected adjusted closes are
literals worked out by hand from the multiple the market moved by:

BANKBEES (INF732E01078), 1:10 unit split, ex 2019-12-19 — 3,286.95 → open 331.63 / close 329.51,
quantity 2,787 → 30,856 against a ~7k median. ``price_factor = 1 / 10 = 0.1``:

    2019-11-15 (pre-split)   3171.51 x 0.1 = 317.1510
    2019-12-18 (pre-split)   3286.95 x 0.1 = 328.6950
    2019-12-19 (ex)           329.51 x 1.0 = 329.5100

TATAMTRDVR (IN9155A01012), 5:1 face-value split (₹10 → ₹2), ex 2011-09-12 — 448.45 → open 85.75
/ close 85.05, quantity ~2x its median. ``price_factor = 2 / 10 = 0.2``:

    2011-08-10 (pre-split)    469.85 x 0.2 =  93.9700
    2011-09-09 (pre-split)    448.45 x 0.2 =  89.6900
    2011-09-12 (ex)            85.05 x 1.0 =  85.0500

Invert the convention and BANKBEES's 2019-12-18 close reads 32,869.50, TATAMTRDVR's 2011-09-09
2,242.25 — neither matches, which is the inversion guard. Offline: L1 is written under `tmp_path`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dataplatform.corpactions.factors import FactorChain
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import PriceRow
from dataplatform.store.l1 import write_prices_raw
from dataplatform.store.l2 import materialize_isin, read_adjusted

BANKBEES = "INF732E01078"
TATAMTRDVR = "IN9155A01012"

#: `(isin, trade_date, open, high, low, close, total_traded_qty)`, NSE EQ, frozen from L1.
_BARS: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    ("IN9155A01012", "2011-08-10", "460.0000", "479.2000", "460.0000", "469.8500", 799058),
    ("IN9155A01012", "2011-08-11", "465.0000", "481.4000", "460.9000", "473.9000", 205901),
    ("IN9155A01012", "2011-08-12", "473.0500", "473.0500", "445.0000", "449.7000", 1462985),
    ("IN9155A01012", "2011-08-16", "456.2000", "458.9500", "431.5000", "436.3000", 323038),
    ("IN9155A01012", "2011-08-17", "436.2000", "438.0000", "418.2500", "423.6500", 465012),
    ("IN9155A01012", "2011-08-18", "420.2000", "430.0000", "412.9000", "418.4000", 647585),
    ("IN9155A01012", "2011-08-19", "413.0000", "416.0000", "400.0000", "405.2500", 965109),
    ("IN9155A01012", "2011-08-22", "429.0000", "429.0000", "401.1500", "411.0000", 607566),
    ("IN9155A01012", "2011-08-23", "415.0000", "415.8500", "403.9000", "406.7000", 309507),
    ("IN9155A01012", "2011-08-24", "409.1000", "411.4000", "397.0000", "401.6500", 366731),
    ("IN9155A01012", "2011-08-25", "402.1500", "415.7000", "402.1000", "411.5500", 672559),
    ("IN9155A01012", "2011-08-26", "415.0000", "425.6000", "404.0000", "407.8500", 604052),
    ("IN9155A01012", "2011-08-29", "415.0500", "428.9000", "412.6500", "424.6000", 489053),
    ("IN9155A01012", "2011-08-30", "430.1000", "439.5000", "418.4500", "429.6500", 423058),
    ("IN9155A01012", "2011-09-02", "424.9500", "442.0000", "422.1500", "435.2500", 534581),
    ("IN9155A01012", "2011-09-05", "421.2000", "442.2500", "421.2000", "439.7000", 595125),
    ("IN9155A01012", "2011-09-06", "435.9500", "454.5000", "433.0000", "448.6500", 667572),
    ("IN9155A01012", "2011-09-07", "449.3000", "459.6000", "442.4000", "448.2500", 347478),
    ("IN9155A01012", "2011-09-08", "449.4500", "464.7500", "449.4500", "462.9000", 267886),
    ("IN9155A01012", "2011-09-09", "464.0000", "465.0000", "447.0000", "448.4500", 196623),
    ("IN9155A01012", "2011-09-12", "85.7500", "87.4000", "82.1000", "85.0500", 989147),
    ("INF732E01078", "2019-11-15", "3144.1100", "3179.3300", "3142.6000", "3171.5100", 20577),
    ("INF732E01078", "2019-11-18", "3160.0000", "3184.4900", "3154.0000", "3157.0400", 13186),
    ("INF732E01078", "2019-11-19", "3179.2800", "3189.9900", "3157.8900", "3186.1000", 3931),
    ("INF732E01078", "2019-11-20", "3126.1200", "3204.9000", "3086.3000", "3145.0400", 32140),
    ("INF732E01078", "2019-11-21", "3180.0000", "3203.0000", "3180.0000", "3192.4800", 20394),
    ("INF732E01078", "2019-11-22", "3184.0000", "3193.0600", "3163.6000", "3168.8000", 4813),
    ("INF732E01078", "2019-11-25", "3168.0000", "3217.8000", "3165.4000", "3214.2100", 6307),
    ("INF732E01078", "2019-11-26", "3217.6000", "3239.9500", "3209.0000", "3231.2700", 13933),
    ("INF732E01078", "2019-11-27", "3221.0000", "3249.6500", "3221.0000", "3244.9900", 5219),
    ("INF732E01078", "2019-11-28", "3250.0000", "3284.0000", "3248.0000", "3278.2600", 55869),
    ("INF732E01078", "2019-11-29", "3277.8900", "3277.9000", "3243.9400", "3256.0700", 3849),
    ("INF732E01078", "2019-12-02", "3257.5500", "3262.0000", "3238.2800", "3249.3500", 16227),
    ("INF732E01078", "2019-12-03", "3249.3500", "3249.3500", "3214.0000", "3221.6600", 11384),
    ("INF732E01078", "2019-12-04", "3215.0000", "3269.0000", "3200.1600", "3259.7700", 5108),
    ("INF732E01078", "2019-12-05", "3269.8500", "3272.0000", "3228.7000", "3235.5300", 10394),
    ("INF732E01078", "2019-12-06", "3235.7000", "3253.3900", "3187.0900", "3198.4400", 17180),
    ("INF732E01078", "2019-12-09", "3244.0000", "3244.0000", "3177.0000", "3198.4800", 1936),
    ("INF732E01078", "2019-12-10", "3186.4100", "3201.5500", "3172.3600", "3177.3600", 9765),
    ("INF732E01078", "2019-12-11", "3181.9500", "3196.2000", "3165.5000", "3187.3000", 2186),
    ("INF732E01078", "2019-12-12", "3196.4600", "3229.8900", "3195.8000", "3224.4800", 7898),
    ("INF732E01078", "2019-12-13", "3238.5500", "3272.9500", "3238.5000", "3264.0600", 13332),
    ("INF732E01078", "2019-12-16", "3274.3500", "3276.0000", "3252.0500", "3260.3900", 2698),
    ("INF732E01078", "2019-12-17", "3266.8500", "3282.7000", "3259.2100", "3276.3400", 2268),
    ("INF732E01078", "2019-12-18", "3276.2300", "3291.0000", "3262.0500", "3286.9500", 2787),
    ("INF732E01078", "2019-12-19", "331.6300", "331.7500", "327.3400", "329.5100", 30856),
)

#: `(isin, date, expected adjusted close)` — the literals above.
_EXPECTED: tuple[tuple[str, str, str], ...] = (
    (BANKBEES, "2019-11-15", "317.1510"),
    (BANKBEES, "2019-12-18", "328.6950"),
    (BANKBEES, "2019-12-19", "329.5100"),
    (TATAMTRDVR, "2011-08-10", "93.9700"),
    (TATAMTRDVR, "2011-09-09", "89.6900"),
    (TATAMTRDVR, "2011-09-12", "85.0500"),
)

_EX_DATES = {BANKBEES: date(2019, 12, 19), TATAMTRDVR: date(2011, 9, 12)}


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    by_date: dict[date, list[PriceRow]] = {}
    for isin, day, o, h, low, c, qty in _BARS:
        trade_date = date.fromisoformat(day)
        by_date.setdefault(trade_date, []).append(
            PriceRow(
                isin=isin,
                symbol="BANKBEES" if isin == BANKBEES else "TATAMTRDVR",
                series="EQ",
                trade_date=trade_date,
                open=Decimal(o),
                high=Decimal(h),
                low=Decimal(low),
                close=Decimal(c),
                last=Decimal(c),
                prev_close=Decimal(c),
                total_traded_qty=qty,
                total_traded_value=Decimal(c) * qty,
                total_trades=1,
            )
        )
    for rows in by_date.values():
        write_prices_raw(rows, exchange=Exchange.NSE, data_root=tmp_path)
    return tmp_path


@pytest.mark.parametrize("isin", [BANKBEES, TATAMTRDVR])
def test_the_implied_split_is_found_on_its_ex_date(lake: Path, isin: str) -> None:
    report = materialize_isin(
        isin, chain=FactorChain(isin=isin, rows=()), actions=(), data_root=lake
    )
    assert [s.ex_date for s in report.implied_splits] == [_EX_DATES[isin]]


@pytest.mark.parametrize(("isin", "day", "expected"), _EXPECTED)
def test_adjusted_closes_match_hand_computed(
    lake: Path, isin: str, day: str, expected: str
) -> None:
    materialize_isin(isin, chain=FactorChain(isin=isin, rows=()), actions=(), data_root=lake)
    by_date = {b.trade_date: b for b in read_adjusted(isin, data_root=lake)}
    bar = by_date[date.fromisoformat(day)]
    assert bar.adj_close == Decimal(expected)
    # adj = raw x factor, row by row, with the factor that was actually applied on the row.
    raw = next(Decimal(r[5]) for r in _BARS if r[0] == isin and r[1] == day)
    assert bar.adj_close == (raw * bar.cum_price_factor).quantize(Decimal("0.0001"))
