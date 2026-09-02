"""HDFC Ltd merger into HDFC Bank, 2023 (EXECUTION_PLAN §4.3).

Published terms: amalgamation of Housing Development Finance Corporation (HDFC Ltd) into HDFC Bank,
exchange ratio 42 equity shares of HDFC Bank (face Rs.1) for every 25 equity shares of HDFC Ltd
(face Rs.2). HDFC Bank is the surviving entity (INE040A01034); HDFC Ltd ceased trading. Effective
2023-07-01, HDFC Bank ex/first-day-post-merger reference 2023-07-13.

Why this is a structural break, not a scaling (§4.3 rule 3): on the surviving HDFC Bank the merger
issues new shares to HDFC Ltd holders but does not sub-divide HDFC Bank's own shares — the crossing
is a structural event, not a return. The engine keeps ``price_factor = 1`` and marks the ex-date a
structural break, so the **return series bridges** it.

Hand-computed adjusted closes (reference B) — unit factor, so adjusted == raw:

    2023-07-12 (pre)    1656.00 x 1.0 = 1656.00
    2023-07-13 (ex)     1631.00 x 1.0 = 1631.00   <- return bridged (structural, not a return)
    2023-07-14 (post)   1642.50 x 1.0 = 1642.50
"""

from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, merger

_ISIN = "INE040A01034"  # HDFC Bank (surviving entity)
_EX = date(2023, 7, 13)

CASE = GoldenCase(
    case_id="hdfc_merger_2023",
    title="HDFC Ltd into HDFC Bank merger (2023)",
    isin=_ISIN,
    published_terms="Amalgamation of HDFC Ltd into HDFC Bank, exchange ratio 42:25 (2023)",
    actions=(
        merger(
            _ISIN,
            _EX,
            received="42",
            held="25",
            raw_text="SCHEME OF AMALGAMATION - HDFC LTD WITH HDFC BANK (42:25)",
        ),
    ),
    expectations=(
        Expectation(date(2023, 7, 12), Decimal("1656.00"), Decimal("1656.00"), "1656.00 x 1.0"),
        Expectation(_EX, Decimal("1631.00"), Decimal("1631.00"), "1631.00 x 1.0 (bridged)"),
        Expectation(date(2023, 7, 14), Decimal("1642.50"), Decimal("1642.50"), "1642.50 x 1.0"),
    ),
    bridged_ex_dates=(_EX,),
)
