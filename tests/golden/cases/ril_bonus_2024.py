"""Reliance Industries 1:1 bonus, 2024 (EXECUTION_PLAN §4.3).

Published terms (NSE/BSE): bonus issue of 1 equity share for every 1 held (1:1). Ex-date
2024-10-28. A holder of 1 share ends with 2.

Factor convention (dataplatform.corpactions.factors): a ``new:held`` bonus has
``price_factor = held / (new + held) = 1 / (1 + 1) = 0.5``. Back-adjustment multiplies every close
*strictly before* the ex-date by 0.5; the ex-date close and later closes are unscaled (factor 1.0).

Hand-computed adjusted closes (reference B). Raw closes are representative EOD levels around the
ex-date; the load-bearing fact is ``adjusted = raw x 0.5`` before the bonus:

    2024-10-25 (cum, pre-bonus)   2658.00 x 0.5 = 1329.00
    2024-10-28 (ex, post-bonus)   1340.00 x 1.0 = 1340.00
    2024-10-29 (post-bonus)       1332.50 x 1.0 = 1332.50

Invert the convention (adjust by 2 instead of 0.5) and 2024-10-25 reads 5316.00, not 1329.00.
"""

from datetime import date
from decimal import Decimal

from tests.golden.casebook import Expectation, GoldenCase, bonus

_ISIN = "INE002A01018"  # Reliance Industries
_EX = date(2024, 10, 28)

CASE = GoldenCase(
    case_id="ril_bonus_2024",
    title="Reliance Industries 1:1 bonus (2024)",
    isin=_ISIN,
    published_terms="Bonus issue 1:1, ex-date 2024-10-28",
    actions=(bonus(_ISIN, _EX, new="1", held="1", raw_text="BONUS 1:1"),),
    expectations=(
        Expectation(date(2024, 10, 25), Decimal("2658.00"), Decimal("1329.00"), "2658.00 x 0.5"),
        Expectation(_EX, Decimal("1340.00"), Decimal("1340.00"), "1340.00 x 1.0"),
        Expectation(date(2024, 10, 29), Decimal("1332.50"), Decimal("1332.50"), "1332.50 x 1.0"),
    ),
)
