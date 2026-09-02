"""IRCTC 1:5 face-value split, 2021 (EXECUTION_PLAN §4.3).

Published terms (NSE/BSE): face value sub-divided from Rs.10 to Rs.2 per share — a 1:5 split, one
Rs.10 share becoming five Rs.2 shares. Ex-date 2021-10-28.

Factor convention (dataplatform.corpactions.factors): a face-value split has
``price_factor = to / from = 2 / 10 = 0.2``. Back-adjustment re-expresses every close *strictly
before* the ex-date in the post-split basis by multiplying it by 0.2; the ex-date close and
everything after it are already in post-split terms (factor 1.0).

Hand-computed adjusted closes (reference B). Raw closes are representative EOD levels around the
ex-date; the load-bearing fact under test is ``adjusted = raw x 0.2`` before the split:

    2021-10-27 (cum, pre-split)   4285.00 x 0.2 = 857.00
    2021-10-28 (ex, post-split)    870.00 x 1.0 = 870.00
    2021-10-29 (post-split)        895.50 x 1.0 = 895.50

Invert the convention (adjust by 5 instead of 0.2) and the 2021-10-27 close reads 21425.00, not
857.00 — which is exactly what the suite's inversion guard proves cannot pass.
"""

from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, split

_ISIN = "INE335Y01020"  # IRCTC
_EX = date(2021, 10, 28)

CASE = GoldenCase(
    case_id="irctc_split_2021",
    title="IRCTC 1:5 face-value split (2021)",
    isin=_ISIN,
    published_terms="Face value split from Rs.10 to Rs.2 (1:5), ex-date 2021-10-28",
    actions=(
        split(_ISIN, _EX, fv_from="10", fv_to="2", raw_text="FV SPLIT FROM RS.10/- TO RS.2/-"),
    ),
    expectations=(
        Expectation(date(2021, 10, 27), Decimal("4285.00"), Decimal("857.00"), "4285.00 x 0.2"),
        Expectation(_EX, Decimal("870.00"), Decimal("870.00"), "870.00 x 1.0"),
        Expectation(date(2021, 10, 29), Decimal("895.50"), Decimal("895.50"), "895.50 x 1.0"),
    ),
)
