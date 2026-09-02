"""Tata Motors DVR ('A' Ordinary) conversion, 2024 (EXECUTION_PLAN §4.3).

Published terms: cancellation of Tata Motors' differential-voting-rights ('A' Ordinary / DVR) shares
and issue of ordinary shares to DVR holders in the ratio 7 ordinary shares for every 10 DVR shares.
On the ordinary Tata Motors line (INE155A01022) this is a DVR_CONVERSION taking effect around
2024-09-02.

Why this is a structural break, not a scaling (§4.3 rule 3): the conversion issues new ordinary
shares to DVR holders but does not sub-divide an existing ordinary holder's shares, so it must not
rescale the ordinary price series — ``price_factor = 1``. The engine records the ex-date as a
structural break, so the **return series bridges** the crossing rather than treating any
capital-side move as a return. The 7:10 ratio governs the DVR-to-ordinary entitlement, not price.

Hand-computed adjusted closes (reference B) — unit factor, so adjusted == raw:

    2024-08-30 (pre)    1005.00 x 1.0 = 1005.00
    2024-09-02 (ex)     1012.00 x 1.0 = 1012.00   <- return bridged (structural)
    2024-09-03 (post)    998.50 x 1.0 =  998.50
"""

from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, dvr_conversion

_ISIN = "INE155A01022"  # Tata Motors (ordinary)
_EX = date(2024, 9, 2)

CASE = GoldenCase(
    case_id="tatamotors_dvr_2024",
    title="Tata Motors DVR conversion (2024)",
    isin=_ISIN,
    published_terms="Conversion of DVR ('A' Ordinary) shares into ordinary, 7:10, ex 2024-09-02",
    actions=(
        dvr_conversion(
            _ISIN,
            _EX,
            received="7",
            held="10",
            raw_text="CONVERSION OF 'A' ORDINARY (DVR) SHARES INTO ORDINARY SHARES (7:10)",
        ),
    ),
    expectations=(
        Expectation(date(2024, 8, 30), Decimal("1005.00"), Decimal("1005.00"), "1005.00 x 1.0"),
        Expectation(_EX, Decimal("1012.00"), Decimal("1012.00"), "1012.00 x 1.0 (bridged)"),
        Expectation(date(2024, 9, 3), Decimal("998.50"), Decimal("998.50"), "998.50 x 1.0"),
    ),
    bridged_ex_dates=(_EX,),
)
