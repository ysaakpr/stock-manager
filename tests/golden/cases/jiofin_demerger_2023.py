"""Jio Financial Services demerger from Reliance Industries, 2023 (EXECUTION_PLAN §4.3).

Published terms: demerger of the financial-services undertaking of Reliance Industries (RIL,
INE002A01018) into Jio Financial Services, entitlement 1 JFS share for every 1 RIL share held.
Ex-date 2023-07-20. NSE ran a special pre-open session that day; JFS was valued at Rs.261.85, and
that value was carved out of RIL's price.

Why this is a structural break, not a return (§4.3 rule 3): the ~Rs.262 fall in RIL's close across
the ex-date is the value of a business RIL no longer contains, not a market return. The engine keeps
``price_factor = 1`` on RIL (the parent's own share basis is unchanged — adjusting it for the
spun-off entity's value needs a different ISIN's price and is not guessed) and marks the ex-date a
structural break, so the **return series bridges** the gap instead of booking a false -9% day.

Hand-computed adjusted closes (reference B) — unit factor, so adjusted == raw:

    2023-07-19 (cum-demerger)   2841.85 x 1.0 = 2841.85
    2023-07-20 (ex)             2580.00 x 1.0 = 2580.00   <- return bridged (JFS carve-out)
    2023-07-21 (post)           2560.30 x 1.0 = 2560.30

Note: this file isolates the demerger; RIL's 2024 1:1 bonus is its own case file. Golden cases pin
one published event each so the arithmetic under test is unambiguous.
"""

from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, demerger

_ISIN = "INE002A01018"  # Reliance Industries (parent)
_EX = date(2023, 7, 20)

CASE = GoldenCase(
    case_id="jiofin_demerger_2023",
    title="Jio Financial demerger from RIL (2023)",
    isin=_ISIN,
    published_terms="Demerger of RIL financial-services business into JFS, 1:1, ex 2023-07-20",
    actions=(
        demerger(
            _ISIN,
            _EX,
            received="1",
            held="1",
            raw_text="DEMERGER OF FINANCIAL SERVICES BUSINESS - JIO FINANCIAL SERVICES (1:1)",
        ),
    ),
    expectations=(
        Expectation(date(2023, 7, 19), Decimal("2841.85"), Decimal("2841.85"), "2841.85 x 1.0"),
        Expectation(_EX, Decimal("2580.00"), Decimal("2580.00"), "2580.00 x 1.0 (bridged)"),
        Expectation(date(2023, 7, 21), Decimal("2560.30"), Decimal("2560.30"), "2560.30 x 1.0"),
    ),
    bridged_ex_dates=(_EX,),
)
