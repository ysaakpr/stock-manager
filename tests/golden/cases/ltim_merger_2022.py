"""LTIMindtree merger — Mindtree into L&T Infotech, 2022 (EXECUTION_PLAN §4.3).

Published terms: scheme of amalgamation of Mindtree Ltd into Larsen & Toubro Infotech Ltd, exchange
ratio 73 equity shares of LTI (face Rs.1) for every 100 equity shares of Mindtree (face Rs.10). LTI
was renamed LTIMindtree; the surviving ISIN INE214T01019 continued trading. Effective/ex 2022-11-24.

Why this is a structural break, not a scaling (§4.3 rule 3): on the surviving entity the merger is
not a sub-division of its own shares — the close-to-close move across the effective date is a
structural event (a different, larger company), not a return. So the engine records
``price_factor = 1`` (the adjusted level series equals the raw series) and marks the ex-date a
structural break, and the **return series bridges** the crossing (``ret = None``) rather than
booking the gap as a spurious return. Adjusting the survivor for the exchange ratio would need the
absorbed entity's price (a different ISIN) and is not guessed.

Hand-computed adjusted closes (reference B) — unit factor, so adjusted == raw:

    2022-11-23 (pre-merger)   4750.00 x 1.0 = 4750.00
    2022-11-24 (effective)    4620.00 x 1.0 = 4620.00   <- return bridged (structural, not a return)
    2022-11-25 (post)         4680.00 x 1.0 = 4680.00
"""

from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, merger

_ISIN = "INE214T01019"  # L&T Infotech -> LTIMindtree (surviving entity)
_EX = date(2022, 11, 24)

CASE = GoldenCase(
    case_id="ltim_merger_2022",
    title="LTIMindtree merger — Mindtree into LTI (2022)",
    isin=_ISIN,
    published_terms="Amalgamation of Mindtree into LTI, exchange ratio 73:100 (2022)",
    actions=(
        merger(
            _ISIN,
            _EX,
            received="73",
            held="100",
            raw_text="SCHEME OF AMALGAMATION - MINDTREE WITH L&T INFOTECH (73:100)",
        ),
    ),
    expectations=(
        Expectation(date(2022, 11, 23), Decimal("4750.00"), Decimal("4750.00"), "4750.00 x 1.0"),
        Expectation(_EX, Decimal("4620.00"), Decimal("4620.00"), "4620.00 x 1.0 (bridged)"),
        Expectation(date(2022, 11, 25), Decimal("4680.00"), Decimal("4680.00"), "4680.00 x 1.0"),
    ),
    bridged_ex_dates=(_EX,),
)
